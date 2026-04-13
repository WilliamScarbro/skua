# SPDX-License-Identifier: BUSL-1.1
"""skua dashboard — live interactive project dashboard."""

import base64
import json
import fcntl
import inspect
import os
import pty
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
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
    _credential_state,
    _base_project_status,
)
from skua.commands.remove import cmd_remove
from skua.commands.restart import cmd_restart
from skua.commands.run import cmd_run
from skua.commands.ssh_cmd import cmd_ssh
from skua.commands.stop import cmd_stop
from skua.config import ConfigStore
from skua.config.resources import ssh_private_keys
from skua.docker import (
    build_image,
    effective_project_image,
    ensure_agent_base_image,
    get_running_skua_containers,
    image_exists,
    image_rebuild_needed,
    image_name_for_project,
    project_uses_agent_base_layer,
    resolve_project_image_inputs,
)
from skua.project_lock import format_project_busy_error, project_busy_error_if_locked
from skua.utils import find_ssh_keys, parse_ssh_config_hosts, select_option

_OSC52_MAX_BYTES = 100_000
_CLIPBOARD_FAST_TIMEOUT_SEC = 0.35
_DASHBOARD_UI_LOG_MAX_BYTES = 2_000_000
_CLIPBOARD_BACKEND_CACHE: str | None = None
_PROJECT_DETAIL_TASKS = {
    "project_ssh_add", "project_ssh_add_manual", "project_ssh_remove",
    "detail_edit_text", "detail_edit_select",
    "detail_source_action", "detail_source_add",
    "detail_ssh_action", "detail_revert",
}


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
    prompt_text: str

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
            "prompt_text": self.prompt_text,
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
            prompt_text=str(data.get("prompt_text", "")),
        )


@dataclass
class BuildPreflightCheck:
    """Preflight build decision for one project."""

    project: str
    needs_rebuild: bool
    force_refresh: bool
    reason: str
    error: str


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


def _active_jobs_for_quit(jobs: list[DashboardJob]) -> list[DashboardJob]:
    """Return jobs that would be orphaned if the dashboard quits now."""
    return [job for job in jobs if job.status in ("queued", "running", "waiting_input")]


def _ssh_key_catalog() -> list[str]:
    keys = [str(p) for p in find_ssh_keys()]
    defaults = ConfigStore().load_global().get("defaults", {})
    global_ssh = str(defaults.get("sshKey", "") or "").strip()
    if global_ssh:
        global_ssh_path = str(Path(global_ssh).expanduser().resolve())
        if Path(global_ssh_path).is_file() and global_ssh_path not in keys:
            keys.append(global_ssh_path)
    return sorted(set(keys))


def _project_detail_fields(project, store: ConfigStore) -> list[dict]:
    """Return structured field descriptors for the editable project detail view.

    Each entry is a dict with at minimum:
      display   - the text to show
      editable  - whether the cursor can land here and Enter activates it
      section   - True for section headers (bold, no cursor)
      action    - task mode to start when activated (for editable fields)
    """
    from skua.commands.source_cmd import explicit_sources

    fields: list[dict] = []

    def _f(display, *, editable=False, section=False, action="", **kw):
        entry = {"display": display, "editable": editable, "section": section, "action": action}
        entry.update(kw)
        return entry

    env = store.load_environment(project.environment)
    network_mode = getattr(getattr(env, "network", None), "mode", "") or "-"
    sources = explicit_sources(project)
    ssh_keys = ssh_private_keys(getattr(project, "ssh", None))
    image = getattr(project, "image", None)
    state = getattr(project, "state", None)
    resource_images = list(getattr(getattr(project, "resources", None), "images", []) or [])

    # ── Header ──
    fields.append(_f(f"Project: {project.name}", section=True))
    fields.append(_f(""))

    # ── References ──
    fields.append(_f("References", section=True))
    fields.append(_f(f"  environment: {project.environment}", editable=True,
                     action="detail_edit_select", field="environment", label="environment",
                     options_kind="Environment"))
    fields.append(_f(f"  security: {project.security}", editable=True,
                     action="detail_edit_select", field="security", label="security profile",
                     options_kind="SecurityProfile"))
    fields.append(_f(f"  agent: {project.agent}", editable=True,
                     action="detail_edit_select", field="agent", label="agent",
                     options_kind="AgentConfig"))
    cred_display = getattr(project, "credential", "") or "(none)"
    fields.append(_f(f"  credential: {cred_display}", editable=True,
                     action="detail_edit_select", field="credential", label="credential",
                     options_kind="Credential", allow_none=True))
    fields.append(_f(f"  network: {network_mode}"))
    fields.append(_f(""))

    # ── Sources ──
    fields.append(_f("Sources", section=True))
    for idx, src in enumerate(sources):
        label = getattr(src, "name", "") or getattr(src, "project", "") or f"source-{idx + 1}"
        loc = getattr(src, "directory", "") or getattr(src, "repo", "") or "-"
        mount = getattr(src, "mount_path", "") or f"/home/dev/{label}"
        host = getattr(src, "host", "") or "local"
        primary = " (primary)" if getattr(src, "primary", False) else ""
        display = f"  [{idx + 1}] {label}{primary}: {loc}  ->  {mount} @ {host}"
        fields.append(_f(display, editable=True, action="detail_source_action", source_idx=idx))
    fields.append(_f("  [+] Add source", editable=True, action="detail_source_add"))
    fields.append(_f(""))

    # ── SSH keys ──
    fields.append(_f("SSH keys", section=True))
    if ssh_keys:
        for idx, key in enumerate(ssh_keys):
            marker = " (primary)" if idx == 0 else ""
            fields.append(_f(f"  {idx + 1}. {key}{marker}", editable=True,
                             action="detail_ssh_action", key_idx=idx, key_path=key))
    else:
        fields.append(_f("  (none)"))
    fields.append(_f("  [+] Add SSH key", editable=True, action="detail_ssh_add"))
    fields.append(_f(""))

    # ── Image ──
    fields.append(_f("Image", section=True))
    fields.append(_f(f"  base_image: {getattr(image, 'base_image', '') or '-'}", editable=True,
                     action="detail_edit_text", field="image.base_image", label="base image"))
    fields.append(_f(f"  from_image: {getattr(image, 'from_image', '') or '-'}", editable=True,
                     action="detail_edit_text", field="image.from_image", label="from image"))
    pkgs = ", ".join(list(getattr(image, "extra_packages", []) or [])) or "-"
    fields.append(_f(f"  extra_packages: {pkgs}", editable=True,
                     action="detail_edit_text", field="image.extra_packages", label="extra packages"))
    fields.append(_f(f"  extra_commands: {len(list(getattr(image, 'extra_commands', []) or []))}"))
    fields.append(_f(f"  version: {int(getattr(image, 'version', 0) or 0)}"))
    fields.append(_f(""))

    # ── Git identity ──
    fields.append(_f("Git", section=True))
    fields.append(_f(f"  name: {getattr(getattr(project, 'git', None), 'name', '') or '-'}", editable=True,
                     action="detail_edit_text", field="git.name", label="git name"))
    fields.append(_f(f"  email: {getattr(getattr(project, 'git', None), 'email', '') or '-'}", editable=True,
                     action="detail_edit_text", field="git.email", label="git email"))
    fields.append(_f(""))

    # ── State (read-only) ──
    fields.append(_f("State", section=True))
    fields.append(_f(f"  status: {getattr(state, 'status', '') or 'idle'}"))
    fields.append(_f(f"  lock_owner: {getattr(state, 'lock_owner', '') or '-'}"))
    fields.append(_f(f"  lock_acquired_at: {getattr(state, 'lock_acquired_at', '') or '-'}"))
    fields.append(_f(""))

    # ── Resources (read-only) ──
    fields.append(_f("Resources", section=True))
    fields.append(_f(f"  images: {', '.join(resource_images) if resource_images else '-'}"))
    fields.append(_f(""))

    # ── Save / Revert actions ──
    fields.append(_f("  [w] Save changes", editable=True, action="detail_save"))
    fields.append(_f("  [u] Revert to checkpoint", editable=True, action="detail_revert"))

    return fields


def _get_nested_field(obj, path: str):
    """Get a field from a dataclass by dotted path."""
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set_nested_field(obj, path: str, value) -> None:
    """Set a field on a dataclass by dotted path, with list coercion for extra_packages."""
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    field = parts[-1]
    if field == "extra_packages" and isinstance(value, str):
        value = [v.strip() for v in value.replace(",", " ").split() if v.strip()]
    setattr(obj, field, value)


def _resolve_skua_cli_prefix() -> list[str]:
    """Return the best command prefix to invoke skua from background jobs."""
    cli_bin = shutil.which("skua")
    if cli_bin:
        return [cli_bin]
    cli_py = Path(__file__).resolve().parents[1] / "cli.py"
    return [sys.executable, str(cli_py)]


def _set_terminal_title(title: str) -> None:
    seq = f"\033]0;{title}\007"
    if os.environ.get("TMUX"):
        seq = f"\033Ptmux;\033{seq}\033\\"
    sys.stdout.write(seq)
    sys.stdout.flush()
    if os.environ.get("INSIDE_EMACS"):
        escaped = title.replace("\\", "\\\\").replace('"', '\\"')
        try:
            subprocess.run(
                ["emacsclient", "--eval", f'(rename-buffer "{escaped}" t)'],
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            pass


def _background_command(action_key: str, project_name: str, discover: bool = False) -> list[str] | None:
    prefix = _resolve_skua_cli_prefix()
    if action_key == "build":
        return prefix + ["build", project_name]
    if action_key == "adapt":
        if discover:
            return prefix + ["adapt", project_name, "--discover", "--force"]
        return prefix + ["adapt", project_name, "--build", "--force"]
    if action_key == "stop":
        return prefix + ["stop", project_name, "--force"]
    if action_key == "remove":
        return prefix + ["remove", project_name]
    return None


def _copy_text_to_clipboard(text: str) -> tuple[bool, str]:
    global _CLIPBOARD_BACKEND_CACHE
    commands = _clipboard_commands()
    terminal_only = (not os.environ.get("DISPLAY")) and (not os.environ.get("WAYLAND_DISPLAY"))
    has_tty = Path("/dev/tty").exists()

    # Preferred order:
    # 1) last known-good backend, 2) OSC52 first for terminal-only sessions, 3) local clipboard commands.
    backends: list[tuple[str, list[str] | None]] = []
    if _CLIPBOARD_BACKEND_CACHE == "osc52" and has_tty:
        backends.append(("osc52", None))
    elif _CLIPBOARD_BACKEND_CACHE:
        cached = next((cmd for cmd in commands if cmd and cmd[0] == _CLIPBOARD_BACKEND_CACHE), None)
        if cached:
            backends.append((cached[0], cached))
    if terminal_only and has_tty:
        backends.append(("osc52", None))
    for cmd in commands:
        backends.append((cmd[0], cmd))
    if has_tty and not terminal_only:
        backends.append(("osc52", None))
    if not backends:
        return False, "clipboard unavailable: no backend and no tty for OSC52"

    last_err = ""
    for backend_name, cmd in backends:
        if backend_name == "osc52":
            ok, detail = _copy_text_to_clipboard_osc52(text)
            if ok:
                _CLIPBOARD_BACKEND_CACHE = "osc52"
                return True, ""
            last_err = detail
            continue
        try:
            result = subprocess.run(
                cmd,
                input=text,
                text=True,
                capture_output=True,
                check=False,
                timeout=_CLIPBOARD_FAST_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            last_err = f"{cmd[0]} timed out"
            continue
        except OSError as exc:
            last_err = str(exc)
            continue
        if result.returncode == 0:
            _CLIPBOARD_BACKEND_CACHE = backend_name
            return True, ""
        last_err = (result.stderr or result.stdout or "").strip() or f"exit {result.returncode}"
    return False, last_err or "clipboard copy failed"


def _clipboard_copy_available() -> bool:
    if _clipboard_commands():
        return True
    return Path("/dev/tty").exists()


def _clipboard_commands() -> list[list[str]]:
    commands: list[list[str]] = []
    has_display = bool(os.environ.get("DISPLAY"))
    has_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if shutil.which("wl-copy") and (has_wayland or os.environ.get("XDG_SESSION_TYPE") == "wayland"):
        commands.append(["wl-copy"])
    if shutil.which("xclip") and has_display:
        commands.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel") and has_display:
        commands.append(["xsel", "--clipboard", "--input"])
    if shutil.which("pbcopy"):
        commands.append(["pbcopy"])
    return commands


def _copy_text_to_clipboard_osc52(text: str) -> tuple[bool, str]:
    try:
        raw = text.encode("utf-8")
    except Exception as exc:
        return False, f"OSC52 encode failed: {exc}"
    if len(raw) > _OSC52_MAX_BYTES:
        return False, f"output too large for OSC52 clipboard ({len(raw)} bytes > {_OSC52_MAX_BYTES})"
    try:
        payload = base64.b64encode(raw).decode("ascii")
    except Exception as exc:
        return False, f"OSC52 encode failed: {exc}"
    seq = f"\033]52;c;{payload}\a"
    # tmux passthrough wrapper.
    if os.environ.get("TMUX"):
        seq = f"\033Ptmux;\033{seq}\033\\"
    tty_path = Path("/dev/tty")
    if not tty_path.exists():
        return False, "OSC52 clipboard unavailable: no tty"
    try:
        with tty_path.open("w", encoding="utf-8", errors="ignore") as tty:
            tty.write(seq)
            tty.flush()
        return True, ""
    except OSError as exc:
        return False, f"OSC52 clipboard unavailable: {exc}"


def _extract_lock_busy_error(lines: list[str]) -> str:
    """Return lock-contention error text from command output lines, if present."""
    for raw in reversed(lines):
        line = str(raw or "").strip()
        if not line:
            continue
        if "Project '" in line and " is busy" in line and "; cannot " in line:
            return line
    return ""


def _lock_block_message(project_name: str, action_key: str) -> str:
    """Return a user-facing lock contention message for an action, if blocked."""
    label = {
        "run": "start this project",
        "build": "build this project",
        "stop": "stop this project",
        "adapt": "adapt this project",
        "remove": "remove this project",
        "restart": "restart this project",
    }.get(str(action_key or "").strip(), "perform this action")

    busy = project_busy_error_if_locked(ConfigStore(), project_name)
    if busy is None:
        return ""
    return format_project_busy_error(busy, label)


def _enqueue_remove_job(jobs: "DashboardJobManager", project_name: str) -> tuple[object | None, str]:
    """Queue a remove job for a project, returning the job or an error message."""
    background = _background_command("remove", project_name)
    if not background:
        return None, "remove action is unavailable"
    try:
        return jobs.enqueue("remove", project_name, command=background), ""
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        return None, f"failed to queue remove {project_name}: {type(exc).__name__}: {exc}"


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
        self._masters: dict[int, int] = {}
        self._buffers: dict[int, str] = {}
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
            if job.status in ("queued", "running", "waiting_input"):
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

    @staticmethod
    def _detect_prompt(buffer: str) -> str:
        marker = re.findall(r"\[\[SKUA_PROMPT\]\]\s*(.+)", buffer)
        if marker:
            return marker[-1].strip()
        tail = buffer[-400:]
        lines = tail.splitlines()
        line = lines[-1] if lines else tail
        if re.search(r"\[[Yy]/[Nn]\]:\s*$", line) or re.search(r"\[[Yy]/n\]:\s*$", line):
            return line.strip()
        if re.search(r"Type 'purge' to confirm:\s*$", line):
            return line.strip()
        return ""

    def enqueue(self, action_key: str, project_name: str, command: list[str] | None = None) -> DashboardJob:
        cmd = command if command is not None else _background_command(action_key, project_name)
        if not cmd:
            raise ValueError(f"Action does not support background execution: {action_key}")
        for existing in self.jobs:
            if (
                existing.project == project_name
                and existing.status in ("queued", "running", "waiting_input")
            ):
                raise ValueError(
                    f"project '{project_name}' already has active job #{existing.job_id} "
                    f"({existing.action})"
                )

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
            prompt_text="",
        )
        self._next_id += 1
        self.jobs.append(job)
        self.jobs = self.jobs[-self.max_jobs:]

        try:
            self._append_log_header(log_path, job)
            master_fd, slave_fd = pty.openpty()
            flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
            fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            proc = subprocess.Popen(
                job.command,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                start_new_session=True,
                close_fds=True,
                env={**os.environ, "SKUA_PROMPT_MODE": "markers"},
            )
            os.close(slave_fd)
            job.pid = proc.pid
            job.status = "running"
            self._processes[job.job_id] = proc
            self._masters[job.job_id] = master_fd
            self._buffers[job.job_id] = ""
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
            if job.status not in ("running", "waiting_input"):
                continue
            proc = self._processes.get(job.job_id)
            if proc is None:
                continue
            master_fd = self._masters.get(job.job_id)
            if master_fd is not None:
                chunk = ""
                while True:
                    ready, _, _ = select.select([master_fd], [], [], 0)
                    if not ready:
                        break
                    try:
                        data = os.read(master_fd, 8192)
                    except BlockingIOError:
                        break
                    except OSError:
                        break
                    if not data:
                        break
                    chunk += data.decode("utf-8", errors="replace")
                if chunk:
                    with Path(job.log_path).open("a", encoding="utf-8") as logf:
                        logf.write(chunk)
                    buf = (self._buffers.get(job.job_id, "") + chunk)[-8000:]
                    self._buffers[job.job_id] = buf
                    if job.status == "waiting_input":
                        job.status = "running"
                        job.prompt_text = ""
                        changed = True
            rc = proc.poll()
            if rc is None:
                if job.status == "running":
                    prompt = self._detect_prompt(self._buffers.get(job.job_id, ""))
                    if prompt:
                        job.status = "waiting_input"
                        job.prompt_text = prompt
                        changed = True
                continue
            job.return_code = rc
            job.ended_at = _utc_now_iso()
            job.status = "success" if rc == 0 else "failed"
            job.prompt_text = ""
            self._append_log_footer(Path(job.log_path), job)
            self._processes.pop(job.job_id, None)
            master_fd = self._masters.pop(job.job_id, None)
            self._buffers.pop(job.job_id, None)
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            changed = True
        if changed:
            self._persist()
        return changed

    def cancel(self, job_id: int) -> bool:
        for job in self.jobs:
            if job.job_id != job_id or job.status not in ("running", "waiting_input"):
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
            job.prompt_text = ""
            self._append_log_footer(Path(job.log_path), job)
            self._processes.pop(job_id, None)
            master_fd = self._masters.pop(job_id, None)
            self._buffers.pop(job_id, None)
            if master_fd is not None:
                try:
                    os.close(master_fd)
                except OSError:
                    pass
            self._persist()
            return True
        return False

    def clear_completed(self) -> int:
        before = len(self.jobs)
        self.jobs = [job for job in self.jobs if job.status in ("queued", "running", "waiting_input")]
        removed = before - len(self.jobs)
        if removed:
            self._persist()
        return removed

    def send_input(self, job_id: int, user_input: str) -> tuple[bool, str]:
        for job in self.jobs:
            if job.job_id != job_id:
                continue
            if job.status not in ("waiting_input", "running"):
                return False, "job is not waiting for input"
            master_fd = self._masters.get(job_id)
            if master_fd is None:
                return False, "job input channel is unavailable"
            try:
                os.write(master_fd, (user_input + "\n").encode("utf-8"))
            except OSError as exc:
                return False, f"failed to send input: {exc}"
            job.status = "running"
            job.prompt_text = ""
            self._buffers[job_id] = ""
            self._persist()
            return True, ""
        return False, "job not found"

    def remove_job(self, job_id: int, delete_log: bool = False) -> tuple[bool, str]:
        for idx, job in enumerate(self.jobs):
            if job.job_id != job_id:
                continue
            if job.status in ("queued", "running", "waiting_input"):
                return False, "job is still running; cancel it first with x"
            self.jobs.pop(idx)
            self._masters.pop(job_id, None)
            self._buffers.pop(job_id, None)
            if delete_log:
                try:
                    Path(job.log_path).unlink(missing_ok=True)
                except OSError:
                    pass
            self._persist()
            return True, ""
        return False, "job not found"

    def list_for_view(self) -> list[DashboardJob]:
        return list(reversed(self.jobs))

    def summary(self) -> str:
        running = sum(1 for j in self.jobs if j.status == "running")
        waiting = sum(1 for j in self.jobs if j.status == "waiting_input")
        failed = sum(1 for j in self.jobs if j.status in ("failed", "orphaned"))
        done = sum(1 for j in self.jobs if j.status in ("success", "failed", "canceled", "orphaned"))
        return f"Jobs: {running} running, {waiting} waiting, {failed} failed/orphaned, {done} completed"

    def tail(self, job: DashboardJob, max_lines: int = 200) -> str:
        path = Path(job.log_path)
        if not path.exists():
            return "(log file not found)"
        lines = path.read_text(errors="replace").splitlines()
        return "\n".join(lines[-max(20, max_lines):]) if lines else "(no output yet)"

    def export_output(self, job: DashboardJob) -> Path:
        export_dir = self.jobs_dir / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = export_dir / f"job-{job.job_id:06d}-{job.action}-{job.project}-{stamp}.txt"
        src = Path(job.log_path)
        if src.exists():
            out.write_text(src.read_text(errors="replace"))
        else:
            out.write_text("(log file not found)\n")
        return out

    def output_lines(self, job: DashboardJob, max_lines: int = 5000) -> list[str]:
        path = Path(job.log_path)
        if not path.exists():
            return ["(log file not found)"]
        lines = path.read_text(errors="replace").splitlines()
        if not lines:
            return ["(no output yet)"]
        return lines[-max(200, max_lines):]


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
    _snap_image_config = g.get("image", {})
    _snap_global_packages = _snap_image_config.get("extraPackages", [])
    _snap_global_commands = _snap_image_config.get("extraCommands", [])

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
            img_name = effective_project_image(image_name_base, project, _snap_global_packages, _snap_global_commands)
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
    stale_credential_count = 0
    rows = []
    for name, project in projects:
        host = getattr(project, "host", "") or ""
        running = _running_for_host(host)
        pending_adapt = _has_pending_adapt_request(project)
        img_name = effective_project_image(image_name_base, project, _snap_global_packages, _snap_global_commands)
        status = _base_project_status(store, project, running, unreachable_hosts, img_name)
        if status.startswith("running"):
            running_count += 1
        if pending_adapt:
            status += "*"
            pending_count += 1
        cred_state, _cred_reason, cred_display = _credential_state(store, project)
        if cred_state in {"missing", "stale"}:
            status += "!"
            stale_credential_count += 1

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
            row.extend([project.agent, cred_display])
        if show_security:
            env = store.load_environment(project.environment)
            network = env.network.mode if env else "?"
            row.extend([project.security, network])
        rows.append({"name": name, "cells": row})

    summary = [f"{len(project_names)} project(s), {running_count} running, {pending_count} pending adapt"]
    if pending_count:
        summary.append("  * pending image-request changes")
    if stale_credential_count:
        summary.append(f"  ! stale/missing local credentials for {stale_credential_count} project(s)")
        summary.append("    run 'skua run <name>' and complete agent login to refresh")
    if show_image and (needs_adapt or needs_build):
        if needs_adapt:
            summary.append("  (A) image-request changes pending; run 'skua adapt'")
        if needs_build:
            summary.append("  (B) image out of date; run 'skua build' or 'skua adapt --build'")
    if show_image and needs_running_image:
        summary.append("  RUNNING-IMAGE indicates a restart is needed to use the latest image")

    return DashboardSnapshot(columns=columns, rows=rows, summary=summary)


def _project_build_preflight(store: ConfigStore, project) -> BuildPreflightCheck:
    g = store.load_global()
    image_name_base = g.get("imageName", "skua-base")
    base_image = g.get("baseImage", "debian:bookworm-slim")
    defaults = g.get("defaults", {})
    build_security_name = defaults.get("security", "open")
    build_security = store.load_security(build_security_name)
    if build_security is None:
        build_security = store.load_security(getattr(project, "security", ""))
    if build_security is None:
        return BuildPreflightCheck(
            project=project.name,
            needs_rebuild=False,
            force_refresh=False,
            reason="",
            error=f"security profile not found for project '{project.name}'",
        )

    agent = store.load_agent(project.agent)
    if agent is None:
        return BuildPreflightCheck(
            project=project.name,
            needs_rebuild=False,
            force_refresh=False,
            reason="",
            error=f"agent config not found for project '{project.name}'",
        )

    image_config = g.get("image", {})
    global_packages = image_config.get("extraPackages", [])
    global_commands = image_config.get("extraCommands", [])
    image_name = effective_project_image(image_name_base, project, global_packages, global_commands)

    # Prebuilt default image — no build step needed, just check existence.
    project_from_image = str(getattr(project.image, "from_image", "") or "").strip()
    if image_name == project_from_image:
        needs_rebuild = not image_exists(image_name)
        return BuildPreflightCheck(
            project=project.name,
            needs_rebuild=needs_rebuild,
            force_refresh=False,
            reason="default image missing" if needs_rebuild else "",
            error="",
        )

    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_packages,
        global_extra_commands=global_commands,
        image_name_base=image_name_base,
    )
    layered_project = project_uses_agent_base_layer(project)
    needs_rebuild, force_refresh, reason = image_rebuild_needed(
        image_name=image_name,
        container_dir=store.get_container_dir(),
        security=build_security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        layer_on_base=layered_project,
    )
    return BuildPreflightCheck(
        project=project.name,
        needs_rebuild=bool(needs_rebuild),
        force_refresh=bool(force_refresh),
        reason=str(reason or ""),
        error="",
    )


def _run_preflight_checks(project_name: str) -> tuple[list[BuildPreflightCheck], list[str]]:
    store = ConfigStore()
    target = store.resolve_project(project_name)
    if target is None:
        return [], [f"project '{project_name}' was not found"]

    target_check = _project_build_preflight(store, target)
    if target_check.error:
        return [], [target_check.error]
    if not target_check.needs_rebuild:
        return [], []

    return [target_check], []


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
    image_name = effective_project_image(image_name_base, project, global_packages, global_commands)

    # Prebuilt default image — no build step needed.
    project_from_image = str(getattr(project.image, "from_image", "") or "").strip()
    if image_name == project_from_image:
        if image_exists(image_name):
            print(f"Image '{image_name}' is already up-to-date (prebuilt default).")
            return True
        print(f"Error: default image '{image_name}' not found locally.")
        print("Rebuild it with: skua default-image build <name>")
        return False

    resolved_base_image, extra_packages, extra_commands = resolve_project_image_inputs(
        default_base_image=base_image,
        agent=agent,
        project=project,
        global_extra_packages=global_packages,
        global_extra_commands=global_commands,
        image_name_base=image_name_base,
    )
    layered_project = project_uses_agent_base_layer(project)
    if layered_project:
        _, success, _, reason = ensure_agent_base_image(
            container_dir=container_dir,
            image_name_base=image_name_base,
            default_base_image=base_image,
            security=security,
            agent=agent,
            global_extra_packages=global_packages,
            global_extra_commands=global_commands,
            quiet=not verbose,
            verbose=verbose,
        )
        if not success:
            print(f"Error: failed to prepare shared agent image for '{project.agent}'.")
            if reason:
                print(reason)
            return False
        if not image_exists(resolved_base_image):
            print(
                f"Error: shared agent image '{resolved_base_image}' is still missing "
                f"after prepare step for project '{project.name}'."
            )
            return False
    needs_rebuild, force_refresh, rebuild_reason = image_rebuild_needed(
        image_name=image_name,
        container_dir=container_dir,
        security=security,
        agent=agent,
        base_image=resolved_base_image,
        extra_packages=extra_packages,
        extra_commands=extra_commands,
        layer_on_base=layered_project,
    )

    if not needs_rebuild:
        print(f"Image '{image_name}' is already up-to-date.")
        return True

    if image_exists(image_name):
        if rebuild_reason:
            print(f"Rebuilding image '{image_name}' for project '{name}' ({rebuild_reason})...")
        else:
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
        pull=force_refresh,
        no_cache=force_refresh,
        layer_on_base=layered_project,
    )
    if success:
        print(f"Build complete for '{image_name}'.")
    else:
        print(f"Build failed for '{image_name}'.")
    return success


def _run_action(action_key: str, project_name: str, replace_process: bool = False) -> bool:
    if action_key == "run":
        try:
            project = ConfigStore().resolve_project(project_name)
            agent_name = project.agent if project else None
        except Exception:
            agent_name = None
        #title = f"skua | {project_name} | {agent_name}" if agent_name else f"skua | {project_name}"
        title = f"<<< skua: {project_name} >>>"
        _set_terminal_title(title)
        try:
            cmd_run(SimpleNamespace(name=project_name, replace_process=replace_process))
        finally:
            _set_terminal_title("<<< skua >>>")
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
        cmd_restart(SimpleNamespace(name=project_name, force=True, replace_process=replace_process))
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


def _matching_credentials(store: ConfigStore, agent_name: str) -> list[str]:
    if not agent_name:
        return []
    return sorted(
        c for c in store.list_resources("Credential")
        if _cred_matches_agent(store, c, agent_name)
    )


def _default_credential_choice(store: ConfigStore, agent_name: str) -> str:
    creds = _matching_credentials(store, agent_name)
    if creds:
        return creds[0]
    return "None (log in in container)"


def _step_enabled(step: int, values: dict) -> bool:
    if step == 3:
        return values.get("source_mode") == "Git repository"
    if step == 4:
        return values.get("source_mode") == "Git repository" and values.get("run_mode") == "Remote SSH host"
    if step == 11:
        return values.get("image_mode") in ("Default image", "Base image")
    return True


def _advance_step(step: int, values: dict) -> int:
    nxt = step + 1
    while nxt <= 11 and not _step_enabled(nxt, values):
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
        "image_mode": "Build new",   # "Build new" | "Default image" | "Base image"
        "default_image": "",
        "image": "",
    }
    step = 0
    while step <= 11:
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
            available_creds = _matching_credentials(store, values["agent"])
            if available_creds:
                options = ["Auto-detect/add local credential", "None (log in in container)"] + available_creds
                default_idx = options.index(available_creds[0])
                status, result = _prompt_select("Credential:", options, default_idx)
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
            agent_defaults = [
                d for d in store.load_all_resources("DefaultImage")
                if not d.agent or d.agent == values["agent"]
            ]
            options = ["Build new"]
            if agent_defaults:
                options.append("Default image")
            options.append("Base image")
            default_idx = options.index(values["image_mode"]) if values["image_mode"] in options else 0
            status, result = _prompt_select("Image source:", options, default_idx)
            if status == "ok":
                values["image_mode"] = result
                if result == "Build new":
                    values["default_image"] = ""
                    values["image"] = ""
        elif step == 11:
            if values["image_mode"] == "Default image":
                agent_defaults = [
                    d for d in store.load_all_resources("DefaultImage")
                    if not d.agent or d.agent == values["agent"]
                ]
                opts = [
                    d.name + (f"  [{d.description}]" if d.description else "")
                    for d in agent_defaults
                ]
                default_idx = 0
                if values["default_image"]:
                    matching = [i for i, o in enumerate(opts) if o.split()[0] == values["default_image"]]
                    if matching:
                        default_idx = matching[0]
                status, result = _prompt_select("Select default image:", opts, default_idx)
                if status == "ok":
                    values["default_image"] = result.split()[0]
                    values["image"] = ""
            elif values["image_mode"] == "Base image":
                status, result = _prompt_text(
                    "Base image override (e.g. ubuntu:24.04)", values["image"], required=False
                )
                if status == "ok":
                    values["image"] = result
                    values["default_image"] = ""

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
        default_image=values["default_image"],
        quick=False,
        no_prompt=True,   # all prompts already handled in wizard steps above
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


def _execute_action(action_key: str, project_name: str, replace_process: bool = False) -> tuple:
    try:
        success = _run_action(action_key, project_name, replace_process=replace_process)
        return bool(success), ""
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return (code == 0), f"Command exited with status {code}."
    except Exception as exc:  # pragma: no cover - defensive runtime guard
        return False, f"{type(exc).__name__}: {exc}"


def _run_action_interactive(
    action_key: str,
    project_name: str,
    suspend=None,
    replace_process: bool = False,
) -> str:
    action_label = {"run": "run", "build": "build", "stop": "stop", "adapt": "adapt", "remove": "remove", "restart": "restart"}[action_key]

    def _run_once() -> tuple[bool, str]:
        print(f"[dashboard] {action_label} {project_name}")
        ok, detail = _execute_action(action_key, project_name, replace_process=replace_process)
        if detail:
            print(detail)
        return ok, detail

    success = False
    detail = ""
    if suspend is None:
        success, detail = _run_once()
    else:
        try:
            with suspend():
                success, detail = _run_once()
        except Exception as exc:
            # Textual inline mode raises SuspendNotSupported; retry without suspend.
            if type(exc).__name__ != "SuspendNotSupported":
                raise
            success, detail = _run_once()
    return f"{action_label} {project_name}: {'ok' if success else 'failed'}"


def cmd_dashboard(args):
    try:
        from rich.console import Group
        from rich.table import Table
        from rich.text import Text
        from textual import events
        from textual.app import App, ComposeResult
        from textual.binding import Binding
        from textual.widgets import Static
        try:
            from textual.widgets import DataTable
        except ImportError:  # pragma: no cover - compatibility for older Textual
            DataTable = None
    except ImportError:
        print("Error: 'skua dashboard' requires the 'textual' package.")
        print("Install it with: pip3 install textual")
        raise SystemExit(1)

    inside_emacs = bool(os.environ.get("INSIDE_EMACS"))
    screen_override = str(os.environ.get("SKUA_DASHBOARD_SCREEN", "")).strip().lower()
    refresh_seconds_raw = getattr(args, "refresh_seconds", 2.0)
    try:
        refresh_seconds = float(refresh_seconds_raw)
    except (TypeError, ValueError):
        print("Error: --refresh-seconds must be a number >= 0.")
        raise SystemExit(2)
    if refresh_seconds < 0:
        print("Error: --refresh-seconds must be >= 0.")
        raise SystemExit(2)
    refresh_label = "off" if refresh_seconds == 0 else f"{refresh_seconds:g}s"

    class DashboardApp(App):
        DEFAULT_CSS = """
        #dashboard-view {
            height: 1fr;
        }
        #dashboard-header {
            height: auto;
        }
        #projects-table {
            height: auto;
            max-height: 14;
        }
        #project-summary {
            height: auto;
        }
        #jobs-header {
            height: auto;
        }
        #jobs-table {
            height: auto;
            max-height: 12;
        }
        #dashboard-footer {
            height: auto;
        }
        #status-bar {
            dock: bottom;
            height: auto;
            width: 100%;
        }
        """
        BINDINGS = [
            Binding("q", "quit", "Quit"),
            Binding("h", "toggle_help", "Help"),
            Binding("tab", "toggle_focus", "Focus"),
            Binding("f", "toggle_focus", "Focus"),
            Binding("v", "toggle_project_detail", "Detail"),
            Binding("k", "add_project_ssh", "Add SSH", show=False),
            Binding("w", "detail_save", "Save", show=False),
            Binding("u", "detail_revert", "Revert", show=False),
            Binding("left", "task_prev_option", show=False),
            Binding("right", "task_next_option", show=False),
            Binding("escape", "task_cancel", show=False),
            Binding("up", "cursor_up", "Up"),
            Binding("down", "cursor_down", "Down"),
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
            Binding("y", "export_job_output", "Export Output"),
        ]

        def __init__(self, dashboard_args):
            super().__init__()
            self.dashboard_args = dashboard_args
            self.snapshot = DashboardSnapshot(columns=[], rows=[], summary=[])
            self.selected = 0
            self.selected_project_name = None
            self.selected_job = 0
            self.focus = "projects"
            self.show_job_output = False
            self.show_help = False
            self.show_project_detail = False
            self.message = ""
            self.jobs = DashboardJobManager()
            self.task_mode = ""
            self.task_step = 0
            self.task_values = {}
            self.task_input = ""
            self.task_option_index = 0
            self.task_catalog = {}
            self.task_job_id = 0
            self.task_error = ""
            self.task_export_options: list[str] = []
            self.task_adapt_project = ""
            self.task_adapt_options: list[str] = []
            self.task_project_name = ""
            self.task_project_options: list[str] = []
            self.task_project_action = ""
            self.output_scroll = 0
            self.output_follow = False
            self.detail_cursor = 0          # index into _project_detail_fields list
            self.detail_draft = None        # Project copy being edited (None = not editing)
            self.detail_original = None     # Project state when detail was opened
            self.detail_checkpoints = []    # list of (iso_timestamp, Project) saved states
            self._detail_close_pending = False  # waiting for second v to discard unsaved changes
            self.project_scroll = 0
            self.project_hscroll = 0
            self.jobs_hscroll = 0
            self._project_table_scroll_x = 0
            self._jobs_table_scroll_x = 0
            self._use_project_widget = DataTable is not None
            self._project_table_sig = None
            self._jobs_table_sig = None
            self._project_cursor_visible = None
            self._jobs_cursor_visible = None
            self._refresh_lock = threading.Lock()
            self._refresh_inflight = False
            self._refresh_pending = False
            self._last_logged_message = ""
            self._job_status_seen = {job.job_id: job.status for job in self.jobs.jobs}
            self._ui_log_path = self.jobs.jobs_dir / "dashboard-ui.log"
            self._resume_mask_until = 0.0
            self._quit_armed_until = 0.0

        def _log_ui_event(self, event: str, **fields) -> None:
            try:
                payload = {"ts": _utc_now_iso(), "event": event}
                payload.update(fields)
                if self._ui_log_path.exists() and self._ui_log_path.stat().st_size > _DASHBOARD_UI_LOG_MAX_BYTES:
                    rotated = self._ui_log_path.with_suffix(".log.1")
                    self._ui_log_path.replace(rotated)
                with self._ui_log_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(payload, sort_keys=True))
                    f.write("\n")
            except Exception:
                # Logging must never impact dashboard interactivity.
                pass

        def _resume_mask_active(self) -> bool:
            return time.monotonic() < float(self._resume_mask_until)

        def _begin_resume_mask(self, seconds: float = 1.0) -> None:
            hold = max(0.05, float(seconds))
            self._resume_mask_until = max(float(self._resume_mask_until), time.monotonic() + hold)
            self.set_timer(hold, self._refresh_view)
            self.set_timer(hold, self._request_refresh)

        def compose(self) -> ComposeResult:
            if self._use_project_widget:
                yield Static(id="dashboard-header")
                yield DataTable(id="projects-table")
                yield Static(id="project-summary")
                yield Static(id="jobs-header")
                yield DataTable(id="jobs-table")
                yield Static(id="dashboard-footer")
            else:
                yield Static(id="dashboard-view")
            yield Static(id="status-bar")

        def on_mount(self) -> None:
            self._log_ui_event("mount", log_path=str(self._ui_log_path))
            if self._use_project_widget:
                self._init_project_table()
                self._init_jobs_table()
            self._request_refresh()
            if refresh_seconds > 0:
                self.set_interval(refresh_seconds, self._request_refresh)

        def _init_project_table(self) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#projects-table")
            try:
                table.cursor_type = "row"
            except Exception:
                pass
            try:
                table.zebra_stripes = False
            except Exception:
                pass
            # Keep global app keybindings authoritative for cursor movement.
            # DataTable should render selection but not consume focus/keys.
            try:
                table.can_focus = False
            except Exception:
                pass

        def _set_project_cursor(self, idx: int) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#projects-table")
            target = max(0, int(idx))
            try:
                table.cursor_row = target
                return
            except Exception:
                pass
            try:
                table.move_cursor(row=target, column=0)
            except Exception:
                pass

        def _init_jobs_table(self) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#jobs-table")
            try:
                table.cursor_type = "row"
            except Exception:
                pass
            try:
                table.zebra_stripes = False
            except Exception:
                pass
            try:
                table.can_focus = False
            except Exception:
                pass

        def _set_jobs_cursor(self, idx: int) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#jobs-table")
            target = max(0, int(idx))
            try:
                table.cursor_row = target
                return
            except Exception:
                pass
            try:
                table.move_cursor(row=target, column=0)
            except Exception:
                pass

        def _refresh_jobs_widget_local(self) -> None:
            if not self._use_project_widget:
                return
            jobs_view = self.jobs.list_for_view()
            self._rebuild_jobs_table(jobs_view)
            self._sync_jobs_cursor_mode()
            self._set_jobs_cursor(self.selected_job)

        def _get_jobs_cursor_row(self) -> int | None:
            if not self._use_project_widget:
                return None
            table = self.query_one("#jobs-table")
            row = getattr(table, "cursor_row", None)
            if isinstance(row, int):
                return row
            coord = getattr(table, "cursor_coordinate", None)
            if coord is not None:
                r = getattr(coord, "row", None)
                if isinstance(r, int):
                    return r
            return None

        def _scroll_table_x(self, table_id: str, delta: int) -> bool:
            """Pan a widget table horizontally by delta cells/chars."""
            if not self._use_project_widget or delta == 0:
                return False
            table = self.query_one(table_id)
            cur_x = int(getattr(table, "scroll_x", 0) or 0)
            next_x = max(0, cur_x + int(delta))
            if next_x == cur_x:
                return False
            cur_y = int(getattr(table, "scroll_y", 0) or 0)
            try:
                table.scroll_to(x=next_x, y=cur_y, animate=False, force=True)
                if table_id == "#projects-table":
                    self._project_table_scroll_x = next_x
                elif table_id == "#jobs-table":
                    self._jobs_table_scroll_x = next_x
                return True
            except Exception:
                return False

        def _restore_table_x(self, table_id: str, x_value: int) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one(table_id)
            cur_y = int(getattr(table, "scroll_y", 0) or 0)
            target_x = max(0, int(x_value))
            try:
                table.scroll_to(x=target_x, y=cur_y, animate=False, force=True)
            except Exception:
                pass
            try:
                table.move_cursor(row=target, column=0)
            except Exception:
                pass

        def _sync_project_cursor_mode(self) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#projects-table")
            show_projects_cursor = (
                self.focus == "projects"
                and not self.task_mode
                and not self.show_job_output
                and not self.show_project_detail
            )
            if self._project_cursor_visible is show_projects_cursor:
                return
            try:
                table.cursor_type = "row" if show_projects_cursor else "none"
                self._project_cursor_visible = show_projects_cursor
            except Exception:
                # Some Textual builds may not support "none"; degrade gracefully.
                if not show_projects_cursor:
                    try:
                        table.show_cursor = False
                        self._project_cursor_visible = False
                    except Exception:
                        pass
                else:
                    try:
                        table.show_cursor = True
                        self._project_cursor_visible = True
                    except Exception:
                        pass

        def _sync_jobs_cursor_mode(self) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#jobs-table")
            show_jobs_cursor = (
                self.focus == "jobs"
                and not self.task_mode
                and not self.show_job_output
                and not self.show_project_detail
            )
            if self._jobs_cursor_visible is show_jobs_cursor:
                return
            try:
                table.cursor_type = "row" if show_jobs_cursor else "none"
                self._jobs_cursor_visible = show_jobs_cursor
            except Exception:
                if not show_jobs_cursor:
                    try:
                        table.show_cursor = False
                        self._jobs_cursor_visible = False
                    except Exception:
                        pass
                else:
                    try:
                        table.show_cursor = True
                        self._jobs_cursor_visible = True
                    except Exception:
                        pass

        def _rebuild_project_table(self) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#projects-table")
            keep_scroll_x = int(self._project_table_scroll_x)
            columns = self._fit_project_columns(self.snapshot.columns)
            rows = self.snapshot.rows or []
            try:
                table.clear(columns=True)
            except Exception:
                try:
                    table.clear()
                except Exception:
                    pass
            if not columns:
                try:
                    table.add_column("PROJECTS")
                    table.add_row("No projects configured.")
                except Exception:
                    pass
                self._project_table_sig = ((), ())
                self._set_project_cursor(0)
                return
            for col_name, _col_width in columns:
                try:
                    table.add_column(col_name)
                except Exception:
                    pass
            for row in rows:
                cells = row.get("cells", [])
                values = []
                for i, (col_name, _col_width) in enumerate(columns):
                    raw_value = str(cells[i]) if i < len(cells) else ""
                    values.append(Text(raw_value, style=self._cell_style(col_name, raw_value)))
                try:
                    table.add_row(*values)
                except Exception:
                    pass
            sig_cols = tuple((name, width) for name, width in columns)
            sig_rows = tuple(tuple(str(cell) for cell in row.get("cells", [])) for row in rows)
            self._project_table_sig = (sig_cols, sig_rows)
            self._restore_table_x("#projects-table", keep_scroll_x)
            self._set_project_cursor(min(self.selected, max(0, len(rows) - 1)))

        def _rebuild_jobs_table(self, jobs_view: list[DashboardJob]) -> None:
            if not self._use_project_widget:
                return
            table = self.query_one("#jobs-table")
            keep_scroll_x = int(self._jobs_table_scroll_x)
            try:
                table.clear(columns=True)
            except Exception:
                try:
                    table.clear()
                except Exception:
                    pass
            columns = [("JOBS", 6), ("ACTION", 8), ("PROJECT", 18), ("STATUS", 14), ("AGE", 6), ("EXIT", 6)]
            for col_name, _col_width in columns:
                try:
                    table.add_column(col_name)
                except Exception:
                    pass
            if not jobs_view:
                try:
                    table.add_row("-", "-", "-", Text("none", style="dim"), "-", "-")
                except Exception:
                    pass
                self._jobs_table_sig = (tuple(c[0] for c in columns), ())
                self._set_jobs_cursor(0)
                return

            visible = jobs_view[:10]
            sig_rows = []
            for job in visible:
                rc_raw = "-" if job.return_code is None else str(job.return_code)
                project_raw = job.project
                status_raw = job.status
                age_raw = _format_age(job.started_at)
                action_raw = job.action
                id_raw = str(job.job_id)
                status_style = self._job_status_style(job.status)
                sig_rows.append((id_raw, action_raw, project_raw, status_raw, age_raw, rc_raw))
                try:
                    table.add_row(
                        id_raw,
                        action_raw,
                        project_raw,
                        Text(status_raw, style=status_style),
                        age_raw,
                        rc_raw,
                    )
                except Exception:
                    pass
            self._jobs_table_sig = (tuple(c[0] for c in columns), tuple(sig_rows))
            self._restore_table_x("#jobs-table", keep_scroll_x)
            self._set_jobs_cursor(min(self.selected_job, max(0, len(visible) - 1)))

        @staticmethod
        def _apply_hscroll(value: str, offset: int) -> str:
            if offset <= 0:
                return value
            if len(value) <= offset:
                return "…"
            return "…" + value[offset:]

        def _max_project_hscroll(self) -> int:
            max_len = 0
            for row in self.snapshot.rows:
                for cell in row.get("cells", []):
                    max_len = max(max_len, len(str(cell)))
            return max(0, max_len - 1)

        def _max_jobs_hscroll(self, jobs_view: list[DashboardJob]) -> int:
            max_len = 0
            for job in jobs_view[:10]:
                rc = "-" if job.return_code is None else str(job.return_code)
                max_len = max(
                    max_len,
                    len(str(job.job_id)),
                    len(job.action),
                    len(job.project),
                    len(job.status),
                    len(_format_age(job.started_at)),
                    len(rc),
                )
            return max(0, max_len - 1)

        def check_action(self, action: str, parameters: tuple[object, ...]) -> bool:  # pragma: no cover - runtime UI behavior
            allowed = True
            if not self.task_mode:
                allowed = True
            elif self.task_mode == "job_input":
                allowed = action in {"run_selected", "task_cancel"}
            elif self.task_mode == "export_choice":
                allowed = action in {"run_selected", "task_cancel", "cursor_up", "cursor_down", "task_prev_option", "task_next_option"}
            elif self.task_mode in _PROJECT_DETAIL_TASKS:
                allowed = action in {
                    "run_selected",
                    "cursor_up",
                    "cursor_down",
                    "task_prev_option",
                    "task_next_option",
                    "task_cancel",
                }
            else:
                allowed = action in {
                    "run_selected",
                    "cursor_up",
                    "cursor_down",
                    "task_prev_option",
                    "task_next_option",
                    "task_cancel",
                }
            self._log_ui_event("check_action", action=action, allowed=allowed, task_mode=self.task_mode)
            return allowed

        def _request_refresh(self) -> None:
            jobs_changed = self.jobs.poll()
            if jobs_changed:
                self._update_job_messages()
            with self._refresh_lock:
                if self._refresh_inflight:
                    self._refresh_pending = True
                    if jobs_changed:
                        self._refresh_view()
                    return
                self._refresh_inflight = True
            thread = threading.Thread(target=self._refresh_worker, daemon=True)
            thread.start()

        def _update_job_messages(self) -> None:
            current = {}
            for job in self.jobs.jobs:
                current[job.job_id] = job.status
                prev = self._job_status_seen.get(job.job_id)
                if prev == job.status:
                    continue
                if job.status != "failed":
                    continue
                lines = self.jobs.output_lines(job, max_lines=160)
                reason = _extract_lock_busy_error(lines)
                if reason:
                    self.message = reason
            self._job_status_seen = current

        def _refresh_worker(self) -> None:
            try:
                snapshot = _collect_snapshot(self.dashboard_args)
                self.call_from_thread(self._apply_snapshot, snapshot)
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                self.call_from_thread(self._apply_refresh_error, f"{type(exc).__name__}: {exc}")

        def _apply_refresh_error(self, detail: str) -> None:
            with self._refresh_lock:
                self._refresh_inflight = False
                rerun = self._refresh_pending
                self._refresh_pending = False
            self.message = f"refresh failed: {detail}"
            self._refresh_view()
            if rerun:
                self._request_refresh()

        def _apply_snapshot(self, snapshot: DashboardSnapshot) -> None:
            with self._refresh_lock:
                self._refresh_inflight = False
                rerun = self._refresh_pending
                self._refresh_pending = False
            prev_name = self.selected_project_name or self._selected_project_name()
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
                self.selected_project_name = self.snapshot.rows[self.selected]["name"]
            else:
                self.selected = 0
                self.selected_project_name = None
            if self.show_project_detail and self.selected_project_name != prev_name:
                self.show_project_detail = False
                self.message = f"project detail closed: '{prev_name}' is no longer available"
            if self._use_project_widget:
                self._project_table_sig = None
                self._jobs_table_sig = None
            self._refresh_view()
            if rerun:
                self._request_refresh()

        def _selected_project_name(self) -> str | None:
            if self.show_project_detail and self.selected_project_name:
                return self.selected_project_name
            if not self.snapshot.rows:
                return None
            if self.selected < 0 or self.selected >= len(self.snapshot.rows):
                return None
            return self.snapshot.rows[self.selected]["name"]

        def _set_selected_project_index(self, idx: int) -> None:
            if not self.snapshot.rows:
                self.selected = 0
                self.selected_project_name = None
                return
            self.selected = max(0, min(int(idx), len(self.snapshot.rows) - 1))
            self.selected_project_name = self.snapshot.rows[self.selected]["name"]

        def _project_detail_name(self) -> str | None:
            if not self.show_project_detail:
                return None
            return self.selected_project_name or self._selected_project_name()

        def _toggle_project_detail(self) -> None:
            if self.task_mode:
                self._refresh_view()
                return
            if self.show_project_detail:
                # Closing: warn about unsaved changes
                if self.detail_draft is not None and self.detail_original is not None:
                    import copy, dataclasses
                    draft_d = dataclasses.asdict(self.detail_draft) if dataclasses.is_dataclass(self.detail_draft) else {}
                    orig_d = dataclasses.asdict(self.detail_original) if dataclasses.is_dataclass(self.detail_original) else {}
                    # Strip read-only state/resources from comparison
                    for d in (draft_d, orig_d):
                        d.pop("state", None)
                        d.pop("resources", None)
                    if draft_d != orig_d:
                        self.message = "unsaved changes — press w to save or press v again to discard"
                        self._detail_close_pending = True
                        self._refresh_view()
                        return
                self._close_project_detail()
                return
            if self.show_job_output or self.focus != "projects":
                self.message = "project detail is available only in projects view"
                self._refresh_view()
                return
            project_name = self._selected_project_name()
            if not project_name:
                self.message = "no project selected"
                self._refresh_view()
                return
            self._open_project_detail(project_name)

        def _open_project_detail(self, project_name: str) -> None:
            import copy
            store = ConfigStore()
            project = store.resolve_project(project_name)
            if project is None:
                self.message = f"project '{project_name}' not found"
                self._refresh_view()
                return
            self.detail_draft = copy.deepcopy(project)
            self.detail_original = copy.deepcopy(project)
            self.detail_checkpoints = []
            self.detail_cursor = 0
            self._detail_close_pending = False
            self.show_project_detail = True
            self.selected_project_name = project_name
            # Advance cursor to first editable field
            fields = _project_detail_fields(self.detail_draft, store)
            editable = [i for i, f in enumerate(fields) if f.get("editable")]
            self.detail_cursor = editable[0] if editable else 0
            self.message = f"project detail: {project_name}  (↑/↓ navigate · ⏎ edit · w save · u revert · v back)"
            self._refresh_view()

        def _close_project_detail(self) -> None:
            self.show_project_detail = False
            self.detail_draft = None
            self.detail_original = None
            self.detail_checkpoints = []
            self._detail_close_pending = False
            name = self.selected_project_name or self._selected_project_name() or "-"
            self.message = f"project list: {name}"
            self._refresh_view()

        def _activate_detail_field(self) -> None:
            """Activate the field at the current detail cursor position."""
            if self.detail_draft is None:
                return
            store = ConfigStore()
            fields = _project_detail_fields(self.detail_draft, store)
            if self.detail_cursor >= len(fields):
                return
            field = fields[self.detail_cursor]
            if not field.get("editable"):
                return
            action = field.get("action", "")
            project_name = self._project_detail_name() or ""

            if action == "detail_edit_select":
                options = store.list_resources(field["options_kind"])
                if field.get("allow_none"):
                    options = ["(none)"] + options
                current = _get_nested_field(self.detail_draft, field["field"])
                self.task_mode = "detail_edit_select"
                self.task_input = ""
                self.task_values = {"field": field["field"], "field_label": field.get("label", field["field"])}
                self.task_project_options = options
                self.task_option_index = options.index(str(current)) if str(current) in options else 0
                self.focus = "task"
                self.message = f"edit {field.get('label', field['field'])}"

            elif action == "detail_edit_text":
                current = _get_nested_field(self.detail_draft, field["field"])
                if isinstance(current, list):
                    current = ", ".join(str(x) for x in current)
                self.task_mode = "detail_edit_text"
                self.task_input = str(current or "")
                self.task_values = {"field": field["field"], "field_label": field.get("label", field["field"])}
                self.focus = "task"
                self.message = f"edit {field.get('label', field['field'])}"

            elif action == "detail_source_action":
                sources = list(getattr(self.detail_draft, "sources", []) or [])
                idx = field.get("source_idx", 0)
                if idx < 0 or idx >= len(sources):
                    self.message = "source not found"
                    self._refresh_view()
                    return
                src = sources[idx]
                options = []
                if not getattr(src, "primary", False):
                    options.append("Set as primary")
                if len(sources) > 1:
                    options.append("Remove")
                options.append("Cancel")
                self.task_mode = "detail_source_action"
                self.task_project_options = options
                self.task_option_index = 0
                self.task_values = {"source_idx": idx}
                self.focus = "task"
                label = getattr(src, "name", "") or getattr(src, "project", "") or f"source-{idx + 1}"
                self.message = f"source '{label}': choose action"

            elif action == "detail_source_add":
                self._start_detail_source_add()

            elif action == "detail_ssh_action":
                key_idx = field.get("key_idx", 0)
                key_path = field.get("key_path", "")
                ssh_keys_list = ssh_private_keys(getattr(self.detail_draft, "ssh", None))
                options = []
                if key_idx > 0:
                    options.append("Set as primary")
                options.extend(["Remove", "Cancel"])
                self.task_mode = "detail_ssh_action"
                self.task_project_options = options
                self.task_option_index = 0
                self.task_values = {"key_idx": key_idx, "key_path": key_path}
                self.focus = "task"
                short = ("…" + key_path[-24:]) if len(key_path) > 26 else key_path
                self.message = f"SSH key {short}: choose action"

            elif action == "detail_ssh_add":
                self._start_project_ssh_add_task(project_name)

            elif action == "detail_save":
                self._do_detail_save()
                return

            elif action == "detail_revert":
                self._start_detail_revert()
                return

            self._refresh_view()

        def _do_detail_save(self) -> None:
            """Persist the in-memory draft to disk, pushing a checkpoint."""
            import copy
            if self.detail_draft is None:
                self.message = "no draft to save"
                self._refresh_view()
                return
            project_name = self._project_detail_name()
            if not project_name:
                self.message = "no project selected"
                self._refresh_view()
                return
            store = ConfigStore()
            # Load current disk version for the checkpoint
            current_disk = store.load_project(project_name)
            if current_disk is not None:
                ts = _utc_now_iso()
                self.detail_checkpoints.append((ts, copy.deepcopy(current_disk)))
                if len(self.detail_checkpoints) > 10:
                    self.detail_checkpoints = self.detail_checkpoints[-10:]
            try:
                store.save_resource(self.detail_draft)
                self.detail_original = copy.deepcopy(self.detail_draft)
                self.message = f"saved '{project_name}'"
            except Exception as exc:
                self.message = f"save failed: {exc}"
            self._refresh_view()
            self._request_refresh()

        def action_detail_save(self) -> None:
            self._log_ui_event("action", name="detail_save")
            if self.task_mode:
                return
            if not self.show_project_detail:
                return
            self._do_detail_save()

        def _start_detail_revert(self) -> None:
            """Show revert options: each saved checkpoint or the original opening state."""
            import dataclasses
            if not self.detail_checkpoints:
                # Just revert to original (no saved checkpoints)
                import copy
                self.detail_draft = copy.deepcopy(self.detail_original)
                self.message = "reverted to original (no checkpoints — save to create one)"
                self._refresh_view()
                return
            options = []
            for ts, _ in reversed(self.detail_checkpoints):
                options.append(f"Checkpoint: {ts}")
            options.append("Cancel")
            self.task_mode = "detail_revert"
            self.task_project_options = options
            self.task_option_index = 0
            self.focus = "task"
            self.message = "revert to checkpoint"
            self._refresh_view()

        def action_detail_revert(self) -> None:
            self._log_ui_event("action", name="detail_revert")
            if self.task_mode:
                return
            if not self.show_project_detail:
                return
            self._start_detail_revert()

        def _start_detail_source_add(self) -> None:
            self.task_mode = "detail_source_add"
            self.task_step = 0
            self.task_values = {
                "source_mode": "Local directory",
                "dir": "",
                "repo": "",
                "name": "",
                "mount_path": "",
            }
            self.task_input = ""
            self.task_error = ""
            self.task_option_index = 0
            self.focus = "task"
            self.message = "add source: choose source type"
            self._sync_detail_source_add_editor()

        def _detail_source_add_steps(self) -> list[dict]:
            steps = [
                {"key": "source_mode", "kind": "select", "label": "Source type",
                 "options": ["Local directory", "Git repository"]},
            ]
            if self.task_values.get("source_mode") == "Local directory":
                steps.append({"key": "dir", "kind": "text", "label": "Directory path", "required": True})
            else:
                steps.append({"key": "repo", "kind": "text", "label": "Repository URL", "required": True})
            steps.append({"key": "name", "kind": "text", "label": "Source name (optional, auto-derived if blank)"})
            steps.append({"key": "mount_path", "kind": "text", "label": "Mount path (optional, e.g. /home/dev/src)"})
            steps.append({"key": "make_primary", "kind": "select", "label": "Make primary?", "options": ["No", "Yes"]})
            steps.append({"key": "confirm", "kind": "select", "label": "Add source",
                          "options": ["Add source", "Cancel"]})
            return steps

        def _detail_source_add_current_step(self) -> dict | None:
            steps = self._detail_source_add_steps()
            if not steps:
                return None
            self.task_step = max(0, min(self.task_step, len(steps) - 1))
            return steps[self.task_step]

        def _sync_detail_source_add_editor(self) -> None:
            step = self._detail_source_add_current_step()
            if step is None:
                return
            key = step["key"]
            if step["kind"] == "text":
                self.task_input = str(self.task_values.get(key, ""))
            else:
                options = step.get("options", [])
                current = self.task_values.get(key, "")
                if options and current in options:
                    self.task_option_index = options.index(current)
                elif options:
                    self.task_option_index = 0
                    self.task_values[key] = options[0]

        def _finish_detail_source_add(self) -> None:
            from pathlib import Path as _Path
            from skua.commands.source_cmd import explicit_sources, sync_project_primary
            from skua.config.resources import ProjectSourceSpec
            tv = self.task_values
            directory = tv.get("dir", "").strip()
            repo = tv.get("repo", "").strip()
            source_name = tv.get("name", "").strip()
            mount_path = tv.get("mount_path", "").strip()
            make_primary = tv.get("make_primary", "No") == "Yes"

            if not source_name:
                if repo:
                    source_name = repo.rstrip("/").split("/")[-1]
                    if source_name.endswith(".git"):
                        source_name = source_name[:-4]
                elif directory:
                    source_name = _Path(directory).name
                else:
                    source_name = "source"
            if not mount_path:
                mount_path = f"/home/dev/{source_name}"

            sources = explicit_sources(self.detail_draft)
            new_source = ProjectSourceSpec(
                project=self.detail_draft.name,
                name=source_name,
                directory=directory,
                repo=repo,
                host="",
                ssh_private_key="",
                mount_path=mount_path,
                primary=make_primary,
            )
            if make_primary:
                for s in sources:
                    s.primary = False
            elif not any(getattr(s, "primary", False) for s in sources):
                new_source.primary = True
            sources.append(new_source)
            self.detail_draft.sources = sources
            sync_project_primary(self.detail_draft, sources)
            self.message = f"added source '{source_name}' (unsaved — press w to save)"
            self._task_cancel(self.message)

        def _build_new_project_catalog(self) -> dict:
            store = ConfigStore()
            g = store.load_global()
            defaults = g.get("defaults", {})
            keys = [str(p) for p in find_ssh_keys()]
            global_ssh = defaults.get("sshKey", "")
            if global_ssh:
                global_ssh_path = str(Path(global_ssh).expanduser().resolve())
                if Path(global_ssh_path).is_file() and global_ssh_path not in keys:
                    keys.append(global_ssh_path)
            return {
                "defaults": defaults,
                "hosts": parse_ssh_config_hosts(),
                "keys": sorted(set(keys)),
                "envs": store.list_resources("Environment"),
                "secs": store.list_resources("SecurityProfile"),
                "agents": store.list_resources("AgentConfig"),
            }

        def _task_steps(self) -> list[dict]:
            steps = [
                {"key": "name", "kind": "text", "label": "Project name", "required": True},
                {"key": "source_mode", "kind": "select", "label": "Project source", "options": ["Local directory", "Git repository"]},
            ]
            if self.task_values.get("source_mode") == "Local directory":
                steps.append({"key": "dir", "kind": "text", "label": "Project directory", "required": True})
            else:
                steps.append({"key": "repo", "kind": "text", "label": "Git repository URL", "required": True})
                steps.append(
                    {
                        "key": "run_mode",
                        "kind": "select",
                        "label": "Run location",
                        "options": ["Local docker host", "Remote SSH host"],
                    }
                )
                if self.task_values.get("run_mode") == "Remote SSH host":
                    host_options = list(self.task_catalog.get("hosts", [])) + ["Manual entry..."]
                    steps.append({"key": "host", "kind": "select", "label": "SSH host", "options": host_options})
                    if self.task_values.get("host") == "Manual entry...":
                        steps.append({"key": "host_manual", "kind": "text", "label": "SSH host", "required": True})

            keys = list(self.task_catalog.get("keys", []))
            if keys:
                steps.append(
                    {
                        "key": "ssh_key",
                        "kind": "select",
                        "label": "SSH private key",
                        "options": keys + ["None", "Manual entry..."],
                    }
                )
                if self.task_values.get("ssh_key") == "Manual entry...":
                    steps.append({"key": "ssh_key_manual", "kind": "text", "label": "SSH private key path"})
            else:
                steps.append({"key": "ssh_key_manual", "kind": "text", "label": "SSH private key path"})

            envs = list(self.task_catalog.get("envs", []))
            secs = list(self.task_catalog.get("secs", []))
            agents = list(self.task_catalog.get("agents", []))
            if envs:
                steps.append({"key": "env", "kind": "select", "label": "Environment", "options": envs + ["Manual entry..."]})
                if self.task_values.get("env") == "Manual entry...":
                    steps.append({"key": "env_manual", "kind": "text", "label": "Environment", "required": True})
            else:
                steps.append({"key": "env_manual", "kind": "text", "label": "Environment", "required": True})
            if secs:
                steps.append({"key": "security", "kind": "select", "label": "Security profile", "options": secs + ["Manual entry..."]})
                if self.task_values.get("security") == "Manual entry...":
                    steps.append({"key": "security_manual", "kind": "text", "label": "Security profile", "required": True})
            else:
                steps.append({"key": "security_manual", "kind": "text", "label": "Security profile", "required": True})
            if agents:
                steps.append({"key": "agent", "kind": "select", "label": "Agent", "options": agents + ["Manual entry..."]})
                if self.task_values.get("agent") == "Manual entry...":
                    steps.append({"key": "agent_manual", "kind": "text", "label": "Agent", "required": True})
            else:
                steps.append({"key": "agent_manual", "kind": "text", "label": "Agent", "required": True})
            agent_name = (
                self.task_values.get("agent_manual", "").strip()
                if self.task_values.get("agent") == "Manual entry..."
                else self.task_values.get("agent", "").strip()
            )
            store = ConfigStore()
            creds = _matching_credentials(store, agent_name)
            steps.append(
                {
                    "key": "credential_choice",
                    "kind": "select",
                    "label": "Credential",
                    "options": ["None (log in in container)"] + creds,
                }
            )
            # Image source: filter default images to those compatible with the selected agent
            agent_defaults = [
                d for d in store.load_all_resources("DefaultImage")
                if not d.agent or d.agent == agent_name
            ]
            image_mode_options = ["Build new"]
            if agent_defaults:
                image_mode_options.append("Default image")
            image_mode_options.append("Base image")
            steps.append({"key": "image_mode", "kind": "select", "label": "Image source", "options": image_mode_options})
            image_mode = self.task_values.get("image_mode", "Build new")
            if image_mode == "Default image":
                default_opts = [
                    d.name + (f"  [{d.description}]" if d.description else "")
                    for d in agent_defaults
                ]
                steps.append({"key": "default_image", "kind": "select", "label": "Default image", "options": default_opts})
            elif image_mode == "Base image":
                steps.append({"key": "image", "kind": "text", "label": "Base image (e.g. ubuntu:24.04)"})
            steps.append({"key": "confirm", "kind": "select", "label": "Create project", "options": ["Create project", "Cancel"]})
            return steps

        def _current_task_step(self) -> dict | None:
            steps = self._task_steps()
            if not steps:
                return None
            self.task_step = max(0, min(self.task_step, len(steps) - 1))
            return steps[self.task_step]

        def _sync_task_editor(self) -> None:
            step = self._current_task_step()
            if step is None:
                return
            key = step["key"]
            if step["kind"] == "text":
                self.task_input = str(self.task_values.get(key, ""))
                return
            options = step.get("options", [])
            current = self.task_values.get(key, "")
            if options and current in options:
                self.task_option_index = options.index(current)
            else:
                if options:
                    if key == "credential_choice" and len(options) > 1:
                        self.task_option_index = 1
                    else:
                        self.task_option_index = 0
                    self.task_values[key] = options[self.task_option_index]
                else:
                    self.task_option_index = 0

        def _start_new_project_task(self) -> None:
            defaults = self.task_catalog.get("defaults", {})
            store = ConfigStore()
            self.task_mode = "new_project"
            self.task_step = 0
            self.task_values = {
                "name": "",
                "source_mode": "Local directory",
                "dir": str(Path.cwd()),
                "repo": "",
                "run_mode": "Local docker host",
                "host": "",
                "host_manual": "",
                "ssh_key": "None",
                "ssh_key_manual": "",
                "env": defaults.get("environment", "local-docker"),
                "env_manual": "",
                "security": defaults.get("security", "open"),
                "security_manual": "",
                "agent": defaults.get("agent", "claude"),
                "agent_manual": "",
                "credential_choice": _default_credential_choice(store, defaults.get("agent", "claude")),
                "image_mode": "Build new",
                "default_image": "",
                "image": "",
                "confirm": "Create project",
            }
            self.focus = "task"
            self.show_job_output = False
            self.task_job_id = 0
            self.task_error = ""
            self._sync_task_editor()
            self.message = "new project: wizard started"

        def _start_job_input_task(self, job: DashboardJob) -> None:
            self.task_mode = "job_input"
            self.task_job_id = job.job_id
            self.task_input = ""
            self.focus = "task"
            self.show_job_output = True
            self.message = f"job #{job.job_id}: waiting for input"

        def _start_export_choice_task(self, job: DashboardJob) -> None:
            options = ["Save to file"]
            if _clipboard_copy_available():
                options.extend(["Copy to clipboard", "Save + clipboard"])
            self.task_mode = "export_choice"
            self.task_job_id = job.job_id
            self.task_export_options = options
            self.task_option_index = 0
            self.focus = "task"
            self.message = f"export job #{job.job_id}"

        def _start_adapt_discover_task(self, project_name: str) -> None:
            self.task_mode = "adapt_discover"
            self.task_adapt_project = project_name
            self.task_adapt_options = ["Discover adaptations (--discover)", "Cancel"]
            self.task_option_index = 0
            self.focus = "task"
            self.show_job_output = False
            self.message = (
                f"project '{project_name}' has no pending image-request changes; "
                "discover adaptations with --discover?"
            )

        def _start_project_ssh_add_task(self, project_name: str) -> None:
            keys = _ssh_key_catalog()
            self.task_mode = "project_ssh_add"
            self.task_project_name = project_name
            self.task_project_options = keys + ["Manual entry...", "Cancel"]
            self.task_option_index = 0
            self.focus = "task"
            self.message = f"project '{project_name}': add SSH key"

        def _start_project_ssh_add_manual_task(self, project_name: str) -> None:
            self.task_mode = "project_ssh_add_manual"
            self.task_project_name = project_name
            self.task_input = ""
            self.focus = "task"
            self.message = f"project '{project_name}': enter SSH key path"

        def _start_project_ssh_remove_task(self, project_name: str) -> None:
            store = ConfigStore()
            project = store.resolve_project(project_name)
            keys = ssh_private_keys(getattr(project, "ssh", None)) if project is not None else []
            if not keys:
                self.message = f"project '{project_name}' has no SSH keys configured"
                self._refresh_view()
                return
            self.task_mode = "project_ssh_remove"
            self.task_project_name = project_name
            self.task_project_options = list(keys) + ["All keys", "Cancel"]
            self.task_option_index = 0
            self.focus = "task"
            self.message = f"project '{project_name}': remove SSH key"

        def _project_has_pending_adapt(self, project_name: str) -> bool:
            store = ConfigStore()
            project = store.resolve_project(project_name)
            if project is None:
                return False
            return bool(_has_pending_adapt_request(project))

        def _project_is_running(self, project_name: str) -> bool:
            store = ConfigStore()
            project = store.resolve_project(project_name)
            if project is None:
                return False
            host = getattr(project, "host", "") or ""
            running = get_running_skua_containers(host=host)
            return f"skua-{project_name}" in set(running or [])

        def _apply_project_ssh_command(self, args: SimpleNamespace, success_message: str) -> None:
            # If detail view is open with a draft, apply the SSH change to the draft
            # rather than writing to disk, keeping it consistent with save/revert workflow.
            if self.show_project_detail and self.detail_draft is not None:
                from skua.config.resources import normalize_project_ssh, ProjectSshSpec
                ssh_keys_list = ssh_private_keys(getattr(self.detail_draft, "ssh", None))
                action = getattr(args, "action", "")
                key = str(getattr(args, "ssh_key", "") or "").strip()
                remove_all = getattr(args, "all", False)
                if action == "add" and key:
                    if key not in ssh_keys_list:
                        ssh_keys_list.append(key)
                    first = ssh_keys_list[0]
                    self.detail_draft.ssh = normalize_project_ssh(
                        ProjectSshSpec(private_key=first, private_keys=ssh_keys_list))
                    self.message = f"{success_message} (unsaved)"
                elif action == "remove":
                    if remove_all:
                        ssh_keys_list = []
                    elif key:
                        ssh_keys_list = [k for k in ssh_keys_list if k != key]
                    first = ssh_keys_list[0] if ssh_keys_list else ""
                    self.detail_draft.ssh = normalize_project_ssh(
                        ProjectSshSpec(private_key=first, private_keys=ssh_keys_list))
                    self.message = f"{success_message} (unsaved)"
                else:
                    self.message = success_message
                self.task_mode = ""
                self.task_project_name = ""
                self.task_project_options = []
                self.task_project_action = ""
                self.focus = "projects"
                self._refresh_view()
                return
            output = StringIO()
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    cmd_ssh(args)
                self.message = success_message
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                detail_lines = [ln for ln in output.getvalue().splitlines() if ln.strip()]
                detail = detail_lines[-1] if detail_lines else f"status {code}"
                self.message = f"{success_message}: failed ({detail})"
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                self.message = f"SSH update failed ({type(exc).__name__}: {exc})"
            self.task_mode = ""
            self.task_project_name = ""
            self.task_project_options = []
            self.task_project_action = ""
            self.focus = "projects"
            self._refresh_view()
            self._request_refresh()

        def _task_cancel(self, reason: str = "new project: cancelled") -> None:
            was_detail_task = self.task_mode in _PROJECT_DETAIL_TASKS
            self.task_mode = ""
            self.task_job_id = 0
            self.task_error = ""
            self.task_export_options = []
            self.task_adapt_project = ""
            self.task_adapt_options = []
            self.task_project_name = ""
            self.task_project_options = []
            self.task_project_action = ""
            self.task_values = {}
            if self.focus == "task":
                if was_detail_task and self.show_project_detail:
                    self.focus = "projects"
                else:
                    self.focus = "projects" if self.snapshot.rows else "jobs"
            self.message = reason

        def _finish_new_project_task(self) -> None:
            host = ""
            if self.task_values.get("source_mode") == "Git repository" and self.task_values.get("run_mode") == "Remote SSH host":
                host = self.task_values.get("host_manual", "").strip() if self.task_values.get("host") == "Manual entry..." else self.task_values.get("host", "").strip()
            ssh_key = ""
            if self.task_values.get("ssh_key") == "Manual entry...":
                ssh_key = self.task_values.get("ssh_key_manual", "").strip()
            elif self.task_values.get("ssh_key") not in ("", "None"):
                ssh_key = self.task_values.get("ssh_key", "").strip()
            env = self.task_values.get("env_manual", "").strip() if self.task_values.get("env") == "Manual entry..." else self.task_values.get("env", "").strip()
            sec = self.task_values.get("security_manual", "").strip() if self.task_values.get("security") == "Manual entry..." else self.task_values.get("security", "").strip()
            agent = self.task_values.get("agent_manual", "").strip() if self.task_values.get("agent") == "Manual entry..." else self.task_values.get("agent", "").strip()
            credential_choice = self.task_values.get("credential_choice", "None (log in in container)")
            no_credential = credential_choice == "None (log in in container)"
            credential_name = "" if no_credential else credential_choice

            image_mode = self.task_values.get("image_mode", "Build new")
            if image_mode == "Default image":
                raw_default = self.task_values.get("default_image", "").strip()
                resolved_default_image = raw_default.split()[0] if raw_default else ""
                resolved_image = ""
            elif image_mode == "Base image":
                resolved_default_image = ""
                resolved_image = self.task_values.get("image", "").strip()
            else:
                resolved_default_image = ""
                resolved_image = ""

            args = SimpleNamespace(
                name=self.task_values.get("name", "").strip(),
                repo=self.task_values.get("repo", "").strip() if self.task_values.get("source_mode") == "Git repository" else "",
                host=host,
                dir=self.task_values.get("dir", "").strip() if self.task_values.get("source_mode") == "Local directory" else "",
                ssh_key=ssh_key,
                env=env,
                security=sec,
                agent=agent,
                image=resolved_image,
                default_image=resolved_default_image,
                quick=False,
                no_prompt=True,
                no_credential=no_credential,
                credential=credential_name,
            )

            self.task_mode = ""
            self.focus = "projects"
            output = StringIO()
            try:
                with redirect_stdout(output), redirect_stderr(output):
                    cmd_add(args)
                self.message = f"new project {args.name}: ok"
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                detail_lines = [ln for ln in output.getvalue().splitlines() if ln.strip()]
                detail = detail_lines[-1] if detail_lines else f"status {code}"
                self.message = f"new project {args.name}: failed ({detail})"
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                self.message = f"new project failed ({type(exc).__name__}: {exc})"
            self._request_refresh()

        def _task_submit_step(self) -> None:
            self._log_ui_event("task_submit", task_mode=self.task_mode, step=self.task_step)
            if self.task_mode == "job_input":
                ok, detail = self.jobs.send_input(self.task_job_id, self.task_input)
                self._log_ui_event("job_input_send", job_id=self.task_job_id, ok=ok, detail=detail)
                if ok:
                    self.message = f"job #{self.task_job_id}: input sent"
                    self.task_mode = ""
                    self.task_input = ""
                    self.task_job_id = 0
                    self.focus = "jobs"
                else:
                    self.message = detail
                self._refresh_view()
                return
            if self.task_mode == "export_choice":
                job = next((j for j in self.jobs.jobs if j.job_id == self.task_job_id), None)
                if job is None:
                    self._task_cancel("export cancelled: job not found")
                    self._refresh_view()
                    return
                option = self.task_export_options[self.task_option_index] if self.task_export_options else "Save to file"
                self._log_ui_event("export_choice", job_id=self.task_job_id, option=option)
                path = self.jobs.export_output(job)
                text = Path(path).read_text(errors="replace")
                if option == "Save to file":
                    self.message = f"exported job #{job.job_id} output to {path}"
                elif option == "Copy to clipboard":
                    ok, detail = _copy_text_to_clipboard(text)
                    self._log_ui_event("clipboard_copy", job_id=job.job_id, ok=ok, detail=detail, bytes=len(text.encode('utf-8', errors='ignore')))
                    self.message = f"copied job #{job.job_id} output to clipboard" if ok else f"clipboard copy failed: {detail}"
                else:
                    ok, detail = _copy_text_to_clipboard(text)
                    self._log_ui_event("clipboard_copy", job_id=job.job_id, ok=ok, detail=detail, bytes=len(text.encode('utf-8', errors='ignore')))
                    if ok:
                        self.message = f"exported to {path} and copied to clipboard"
                    else:
                        self.message = f"exported to {path}; clipboard copy failed: {detail}"
                self.task_mode = ""
                self.task_job_id = 0
                self.task_export_options = []
                self.focus = "jobs"
                self._refresh_view()
                return
            if self.task_mode == "adapt_discover":
                project_name = self.task_adapt_project
                option = (
                    self.task_adapt_options[self.task_option_index]
                    if self.task_adapt_options
                    else "Cancel"
                )
                if option.startswith("Discover"):
                    lock_msg = _lock_block_message(project_name, "adapt")
                    if lock_msg:
                        self.message = lock_msg
                        self._refresh_view()
                        return
                    background = _background_command("adapt", project_name, discover=True)
                    if not background:
                        self.message = "adapt action is unavailable"
                        self._refresh_view()
                        return
                    try:
                        job = self.jobs.enqueue("adapt", project_name, command=background)
                        self.message = f"queued job #{job.job_id}: adapt {project_name} --discover"
                        self.show_job_output = False
                        self.selected_job = 0
                    except Exception as exc:
                        self.message = (
                            f"failed to queue adapt {project_name} --discover: "
                            f"{type(exc).__name__}: {exc}"
                        )
                else:
                    self.message = f"adapt cancelled for '{project_name}'"
                self.task_mode = ""
                self.task_adapt_project = ""
                self.task_adapt_options = []
                self.focus = "projects"
                self._refresh_view()
                self._request_refresh()
                return
            if self.task_mode == "project_ssh_add":
                project_name = self.task_project_name
                option = (
                    self.task_project_options[self.task_option_index]
                    if self.task_project_options
                    else "Cancel"
                )
                if option == "Cancel":
                    self._task_cancel(f"SSH key add cancelled for '{project_name}'")
                    self._refresh_view()
                    return
                if option == "Manual entry...":
                    self._start_project_ssh_add_manual_task(project_name)
                    self._refresh_view()
                    return
                self._apply_project_ssh_command(
                    SimpleNamespace(action="add", name=project_name, ssh_key=option, no_prompt=True),
                    f"project '{project_name}': SSH key added",
                )
                return
            if self.task_mode == "project_ssh_add_manual":
                project_name = self.task_project_name
                key_path = self.task_input.strip()
                if not key_path:
                    self.message = "SSH private key path is required"
                    self._refresh_view()
                    return
                self._apply_project_ssh_command(
                    SimpleNamespace(action="add", name=project_name, ssh_key=key_path, no_prompt=True),
                    f"project '{project_name}': SSH key added",
                )
                return
            if self.task_mode == "project_ssh_remove":
                project_name = self.task_project_name
                option = (
                    self.task_project_options[self.task_option_index]
                    if self.task_project_options
                    else "Cancel"
                )
                if option == "Cancel":
                    self._task_cancel(f"SSH key removal cancelled for '{project_name}'")
                    self._refresh_view()
                    return
                if option == "All keys":
                    args = SimpleNamespace(action="remove", name=project_name, ssh_key="", all=True, no_prompt=True)
                else:
                    args = SimpleNamespace(action="remove", name=project_name, ssh_key=option, all=False, no_prompt=True)
                self._apply_project_ssh_command(
                    args,
                    f"project '{project_name}': SSH keys updated",
                )
                return
            if self.task_mode == "detail_edit_select":
                options = self.task_project_options or []
                value = options[self.task_option_index] if options else ""
                if value == "(none)":
                    value = ""
                field = self.task_values.get("field", "")
                label = self.task_values.get("field_label", field)
                if field and self.detail_draft is not None:
                    _set_nested_field(self.detail_draft, field, value)
                    self.message = f"set {label}: {value or '(none)'}"
                self._task_cancel(self.message)
                self._refresh_view()
                return

            if self.task_mode == "detail_edit_text":
                value = self.task_input
                field = self.task_values.get("field", "")
                label = self.task_values.get("field_label", field)
                if field and self.detail_draft is not None:
                    _set_nested_field(self.detail_draft, field, value)
                    display = value if not isinstance(_get_nested_field(self.detail_draft, field), list) else str(_get_nested_field(self.detail_draft, field))
                    self.message = f"set {label}: {display}"
                self._task_cancel(self.message)
                self._refresh_view()
                return

            if self.task_mode == "detail_source_action":
                option = (self.task_project_options[self.task_option_index]
                          if self.task_project_options else "Cancel")
                if option == "Cancel":
                    self._task_cancel("source action cancelled")
                    self._refresh_view()
                    return
                from skua.commands.source_cmd import explicit_sources, sync_project_primary
                sources = explicit_sources(self.detail_draft)
                idx = int(self.task_values.get("source_idx", 0))
                if option == "Set as primary":
                    for i, s in enumerate(sources):
                        s.primary = (i == idx)
                    self.detail_draft.sources = sources
                    sync_project_primary(self.detail_draft, sources)
                    label = getattr(sources[idx], "name", "") or getattr(sources[idx], "project", "") or f"source-{idx + 1}"
                    self.message = f"'{label}' is now primary (unsaved)"
                elif option == "Remove":
                    if len(sources) <= 1:
                        self.message = "cannot remove the only source"
                        self._task_cancel(self.message)
                        self._refresh_view()
                        return
                    removed = sources.pop(idx)
                    if getattr(removed, "primary", False) and sources:
                        sources[0].primary = True
                    self.detail_draft.sources = sources
                    sync_project_primary(self.detail_draft, sources)
                    label = getattr(removed, "name", "") or getattr(removed, "project", "") or f"source-{idx + 1}"
                    self.message = f"removed source '{label}' (unsaved)"
                self._task_cancel(self.message)
                self._refresh_view()
                return

            if self.task_mode == "detail_source_add":
                step = self._detail_source_add_current_step()
                if step is None:
                    return
                key = step["key"]
                if step["kind"] == "text":
                    value = self.task_input.strip() if step.get("required") else self.task_input
                    if step.get("required") and not value:
                        self.message = f"{step['label']} is required"
                        self.task_error = self.message
                        self._refresh_view()
                        return
                    if key == "dir" and value:
                        from pathlib import Path as _Path
                        if not _Path(value).expanduser().exists():
                            self.message = "directory does not exist"
                            self.task_error = self.message
                            self._refresh_view()
                            return
                    self.task_values[key] = value
                    self.task_error = ""
                else:
                    options = step.get("options", [])
                    if options:
                        self.task_values[key] = options[self.task_option_index]
                if key == "confirm":
                    if self.task_values.get("confirm") == "Add source":
                        self._finish_detail_source_add()
                    else:
                        self._task_cancel("add source cancelled")
                    self._refresh_view()
                    return
                self.task_step += 1
                self.task_error = ""
                self._sync_detail_source_add_editor()
                self._refresh_view()
                return

            if self.task_mode == "detail_ssh_action":
                option = (self.task_project_options[self.task_option_index]
                          if self.task_project_options else "Cancel")
                if option == "Cancel":
                    self._task_cancel("SSH key action cancelled")
                    self._refresh_view()
                    return
                from skua.config.resources import normalize_project_ssh, ProjectSshSpec
                key_path = self.task_values.get("key_path", "")
                key_idx = int(self.task_values.get("key_idx", 0))
                ssh_keys_list = ssh_private_keys(getattr(self.detail_draft, "ssh", None))
                if option == "Set as primary":
                    if key_path in ssh_keys_list:
                        ssh_keys_list.remove(key_path)
                    ssh_keys_list.insert(0, key_path)
                    self.detail_draft.ssh = normalize_project_ssh(
                        ProjectSshSpec(private_key=ssh_keys_list[0], private_keys=ssh_keys_list))
                    self.message = f"SSH key set as primary (unsaved)"
                elif option == "Remove":
                    ssh_keys_list = [k for k in ssh_keys_list if k != key_path]
                    first = ssh_keys_list[0] if ssh_keys_list else ""
                    self.detail_draft.ssh = normalize_project_ssh(
                        ProjectSshSpec(private_key=first, private_keys=ssh_keys_list))
                    self.message = f"SSH key removed (unsaved)"
                self._task_cancel(self.message)
                self._refresh_view()
                return

            if self.task_mode == "detail_revert":
                option = (self.task_project_options[self.task_option_index]
                          if self.task_project_options else "Cancel")
                if option == "Cancel":
                    self._task_cancel("revert cancelled")
                    self._refresh_view()
                    return
                import copy
                # options are in reversed checkpoint order: last saved = index 0
                chosen_rev_idx = self.task_option_index
                rev_checkpoints = list(reversed(self.detail_checkpoints))
                if chosen_rev_idx < len(rev_checkpoints):
                    ts, cp_project = rev_checkpoints[chosen_rev_idx]
                    self.detail_draft = copy.deepcopy(cp_project)
                    self.message = f"reverted to checkpoint {ts} (unsaved — press w to keep)"
                else:
                    self.message = "invalid checkpoint selection"
                self._task_cancel(self.message)
                self._refresh_view()
                return

            if self.task_mode != "new_project":
                return
            step = self._current_task_step()
            if step is None:
                return
            key = step["key"]
            if step["kind"] == "text":
                value = self.task_input.strip() if step.get("required", False) else self.task_input
                if step.get("required", False) and not value:
                    self.message = f"{step['label']} is required"
                    self._refresh_view()
                    return
                if key == "name":
                    if not all(c.isalnum() or c in "-_" for c in value):
                        self.task_error = "invalid project name"
                        self.message = "project name must be alphanumeric (hyphens/underscores allowed)"
                        self._refresh_view()
                        return
                    if ConfigStore().load_project(value) is not None:
                        self.task_error = "duplicate project name"
                        self.message = "duplicate project name"
                        self._refresh_view()
                        return
                    self.task_error = ""
                if key == "dir":
                    if not Path(value).expanduser().exists():
                        self.task_error = "directory does not exist"
                        self.message = "directory does not exist"
                        self._refresh_view()
                        return
                    self.task_error = ""
                self.task_values[key] = value
            else:
                options = step.get("options", [])
                if options:
                    self.task_values[key] = options[self.task_option_index]

            if key == "confirm":
                if self.task_values.get("confirm") == "Create project":
                    self._finish_new_project_task()
                else:
                    self._task_cancel()
                self._refresh_view()
                return

            self.task_step += 1
            self.task_error = ""
            self._sync_task_editor()
            self._refresh_view()

        def _task_shift_option(self, delta: int) -> None:
            if self.task_mode == "export_choice":
                if not self.task_export_options:
                    return
                self.task_option_index = (self.task_option_index + delta) % len(self.task_export_options)
                self._refresh_view()
                return
            if self.task_mode == "adapt_discover":
                if not self.task_adapt_options:
                    return
                self.task_option_index = (self.task_option_index + delta) % len(self.task_adapt_options)
                self._refresh_view()
                return
            if self.task_mode in {
                "project_ssh_add", "project_ssh_remove",
                "detail_edit_select", "detail_source_action",
                "detail_ssh_action", "detail_revert",
            }:
                if not self.task_project_options:
                    return
                self.task_option_index = (self.task_option_index + delta) % len(self.task_project_options)
                self._refresh_view()
                return
            if self.task_mode == "detail_source_add":
                step = self._detail_source_add_current_step()
                if step is None or step["kind"] != "select":
                    return
                options = step.get("options", [])
                if not options:
                    return
                self.task_option_index = (self.task_option_index + delta) % len(options)
                self.task_values[step["key"]] = options[self.task_option_index]
                self._refresh_view()
                return
            step = self._current_task_step()
            if step is None or step["kind"] != "select":
                return
            options = step.get("options", [])
            if not options:
                return
            self.task_option_index = (self.task_option_index + delta) % len(options)
            self.task_values[step["key"]] = options[self.task_option_index]
            self._refresh_view()

        def on_key(self, event) -> None:  # pragma: no cover - runtime UI behavior
            self._log_ui_event("key", key=getattr(event, "key", ""), character=getattr(event, "character", None), task_mode=self.task_mode)
            if not self.task_mode:
                return
            if getattr(event, "key", "") == "backspace":
                if self.task_input:
                    self.task_input = self.task_input[:-1]
                    if self.task_mode == "new_project":
                        step = self._current_task_step()
                        if step is not None and step.get("key") == "name":
                            self.task_error = ""
                    self._refresh_view()
                event.stop()
                return
            if getattr(event, "key", "") == "delete":
                if self.task_mode == "new_project" and self.task_step > 0:
                    self.task_step -= 1
                    self.task_input = ""
                    self.task_error = ""
                    self._sync_task_editor()
                    self._refresh_view()
                    event.stop()
                    return
                if self.task_mode == "detail_source_add" and self.task_step > 0:
                    self.task_step -= 1
                    self.task_input = ""
                    self.task_error = ""
                    self._sync_detail_source_add_editor()
                    self._refresh_view()
                    event.stop()
                    return
                return
            if self.task_mode == "job_input":
                ch = getattr(event, "character", None)
                if not ch or len(ch) != 1 or ch in ("\n", "\r", "\t"):
                    return
                self.task_input += ch
                self._refresh_view()
                event.stop()
                return
            if self.task_mode in ("project_ssh_add_manual", "detail_edit_text"):
                ch = getattr(event, "character", None)
                if not ch or len(ch) != 1 or ch in ("\n", "\r", "\t"):
                    return
                self.task_input += ch
                self._refresh_view()
                event.stop()
                return
            if self.task_mode == "detail_source_add":
                step = self._detail_source_add_current_step()
                if step is None or step["kind"] != "text":
                    return
                ch = getattr(event, "character", None)
                if not ch or len(ch) != 1 or ch in ("\n", "\r", "\t"):
                    return
                self.task_input += ch
                self.task_error = ""
                self._refresh_view()
                event.stop()
                return
            step = self._current_task_step()
            if step is None or step["kind"] != "text":
                return
            ch = getattr(event, "character", None)
            if not ch or len(ch) != 1 or ch in ("\n", "\r", "\t"):
                return
            self.task_input += ch
            if step.get("key") == "name":
                self.task_error = ""
            self._refresh_view()
            event.stop()

        def on_paste(self, event: events.Paste) -> None:  # pragma: no cover - runtime UI behavior
            self._log_ui_event("paste", length=len((event.text or "")), task_mode=self.task_mode)
            if not self.task_mode:
                return
            pasted = (event.text or "").replace("\r", "")
            if not pasted:
                return
            if self.task_mode == "job_input":
                self.task_input += pasted
                self._refresh_view()
                event.stop()
                return
            if self.task_mode in ("project_ssh_add_manual", "detail_edit_text"):
                self.task_input += pasted
                self._refresh_view()
                event.stop()
                return
            if self.task_mode == "detail_source_add":
                step = self._detail_source_add_current_step()
                if step is not None and step["kind"] == "text":
                    self.task_input += pasted
                    self.task_error = ""
                    self._refresh_view()
                    event.stop()
                return
            step = self._current_task_step()
            if step is None or step["kind"] != "text":
                return
            self.task_input += pasted
            if step.get("key") == "name":
                self.task_error = ""
            self._refresh_view()
            event.stop()

        def _refresh_view(self) -> None:
            if self.message != self._last_logged_message:
                self._log_ui_event("message", value=self.message)
                self._last_logged_message = self.message
            if self._use_project_widget:
                self._refresh_view_with_project_widget()
                return
            view = self.query_one("#dashboard-view", Static)
            status_bar = self.query_one("#status-bar", Static)
            title = self._render_header_line("skua dashboard", f"auto-refresh: {refresh_label}")
            jobs_view = self.jobs.list_for_view()
            visible_jobs = min(len(jobs_view), 10)
            if jobs_view:
                self.selected_job = min(max(0, self.selected_job), visible_jobs - 1)
            else:
                self.selected_job = 0
                if self.show_job_output:
                    self.show_job_output = False
                    self.output_follow = False
            if self.focus == "jobs" and not jobs_view and self.snapshot.rows:
                self.focus = "projects"
                self.show_job_output = False
                self.output_follow = False
            if self.focus == "projects" and not self.snapshot.rows and jobs_view:
                self.focus = "jobs"
            if self.show_job_output and jobs_view:
                selected_job = jobs_view[self.selected_job]
                if selected_job.status == "waiting_input" and self.task_mode != "job_input":
                    self._start_job_input_task(selected_job)
                if self.task_mode == "job_input" and selected_job.job_id != self.task_job_id:
                    self.task_mode = ""
                    self.task_job_id = 0
            focus_line = Text(f"Focus: {self.focus} | {self.jobs.summary()}", style="cyan bold")
            status_bar.update(
                Group(
                    self._render_task_panel(),
                    self._render_command_bar(self._context_actions(jobs_view)),
                )
            )
            if self._resume_mask_active():
                waiting = Text("Restoring dashboard layout...", style="bold white on black")
                view.update(
                    Group(
                        title,
                        focus_line,
                        self._section_header("Please Wait"),
                        waiting,
                    )
                )
                return

            if self.show_help:
                help_text = Text(
                    "Keys: Up/Down select | Enter run | b build | s stop | a adapt | d remove | r restart | v detail | n new\n"
                    "      tab/f focus projects/jobs | o output | x cancel job | c clear completed jobs | y export output\n"
                    "      h toggle help | q quit"
                )
                view.update(
                    Group(
                        title,
                        focus_line,
                        self._section_header("Help"),
                        help_text,
                    )
                )
                return

            if self.show_project_detail:
                import dataclasses
                dirty = (
                    self.detail_draft is not None and self.detail_original is not None
                    and dataclasses.is_dataclass(self.detail_draft)
                    and dataclasses.asdict(self.detail_draft) != dataclasses.asdict(self.detail_original)
                )
                dirty_tag = "  * unsaved" if dirty else ""
                detail_title = Text(
                    f"Project Detail: {self._project_detail_name() or '-'}{dirty_tag}  (v back · w save · u revert)",
                    style="bold",
                )
                view.update(
                    Group(
                        title,
                        focus_line,
                        self._section_header("Project Detail"),
                        detail_title,
                        Text(""),
                        self._render_project_detail(),
                    )
                )
                return

            if self.show_job_output and jobs_view:
                selected = jobs_view[self.selected_job]
                log_title = Text(
                    f"Job #{selected.job_id} {selected.action} {selected.project} [{selected.status}] (press o to close)",
                    style="bold",
                )
                lines = self.jobs.output_lines(selected)
                available, _max_scroll = self._sync_output_window(lines)
                window = lines[self.output_scroll:self.output_scroll + available]
                scroll_hint = (
                    f"lines {self.output_scroll + 1}-{self.output_scroll + len(window)} of {len(lines)}"
                    if lines else "no output"
                )
                log_text = Text("\n".join(window), style="white")
                view.update(
                    Group(
                        title,
                        focus_line,
                        self._section_header("Job Output"),
                        log_title,
                        Text(scroll_hint, style="dim"),
                        Text(""),
                        log_text,
                    )
                )
                return

            content: list = [title, focus_line, self._section_header("Projects")]
            if not self.snapshot.rows:
                content.append(Text("No projects configured.", style="dim"))
            else:
                # Keep the projects pane height bounded so terminal redraws do not
                # force viewport jumps in hosts like Emacs term buffers.
                viewport_h = (getattr(self, "size", None).height or 24)
                summary_lines = max(1, len(self.snapshot.summary))
                jobs_rows = min(len(jobs_view), 10) if jobs_view else 1
                reserved_lines = 11 + summary_lines + jobs_rows
                visible_projects = max(3, viewport_h - reserved_lines)
                visible_projects = min(visible_projects, len(self.snapshot.rows))

                max_scroll = max(0, len(self.snapshot.rows) - visible_projects)
                self.project_scroll = max(0, min(self.project_scroll, max_scroll))
                if self.selected < self.project_scroll:
                    self.project_scroll = self.selected
                elif self.selected >= self.project_scroll + visible_projects:
                    self.project_scroll = self.selected - visible_projects + 1

                start = self.project_scroll
                end = min(len(self.snapshot.rows), start + visible_projects)
                project_rows = self.snapshot.rows[start:end]

                fitted_columns = self._fit_project_columns(self.snapshot.columns)
                table = Table(box=None, show_edge=False, pad_edge=False)
                for col_name, col_width in fitted_columns:
                    table.add_column(col_name, width=col_width, overflow="ellipsis", no_wrap=True)
                for rel_idx, row in enumerate(project_rows):
                    abs_idx = start + rel_idx
                    style = "reverse" if self.focus == "projects" and abs_idx == self.selected else ""
                    rendered_cells = []
                    for col_index, cell in enumerate(row["cells"]):
                        col_name = fitted_columns[col_index][0] if col_index < len(fitted_columns) else ""
                        raw = str(cell)
                        rendered_cells.append(Text(self._apply_hscroll(raw, self.project_hscroll), style=self._cell_style(col_name, raw)))
                    table.add_row(*rendered_cells, style=style)
                content.append(table)

            summary = Text()
            for idx, line in enumerate(self.snapshot.summary):
                summary.append(line, style=self._summary_style(line))
                if idx < len(self.snapshot.summary) - 1:
                    summary.append("\n")
            jobs_table = self._render_jobs_table(jobs_view)
            content.extend(
                [
                    Text(""),
                    self._section_header("Project Summary"),
                    summary,
                    Text(""),
                    self._section_header("Jobs"),
                    jobs_table,
                ]
            )
            view.update(Group(*content))

        def _refresh_view_with_project_widget(self) -> None:
            header = self.query_one("#dashboard-header", Static)
            projects_table = self.query_one("#projects-table")
            project_summary = self.query_one("#project-summary", Static)
            jobs_header = self.query_one("#jobs-header", Static)
            jobs_table_view = self.query_one("#jobs-table")
            footer = self.query_one("#dashboard-footer", Static)
            status_bar = self.query_one("#status-bar", Static)
            title = self._render_header_line("skua dashboard", f"auto-refresh: {refresh_label}")
            jobs_view = self.jobs.list_for_view()
            visible_jobs = min(len(jobs_view), 10)
            if jobs_view:
                self.selected_job = min(max(0, self.selected_job), visible_jobs - 1)
            else:
                self.selected_job = 0
                if self.show_job_output:
                    self.show_job_output = False
                    self.output_follow = False
            if self.focus == "jobs" and not jobs_view and self.snapshot.rows:
                self.focus = "projects"
                self.show_job_output = False
                self.output_follow = False
            if self.focus == "projects" and not self.snapshot.rows and jobs_view:
                self.focus = "jobs"
            if self.show_job_output and jobs_view:
                selected_job = jobs_view[self.selected_job]
                if selected_job.status == "waiting_input" and self.task_mode != "job_input":
                    self._start_job_input_task(selected_job)
                if self.task_mode == "job_input" and selected_job.job_id != self.task_job_id:
                    self.task_mode = ""
                    self.task_job_id = 0

            self._sync_project_cursor_mode()
            self._sync_jobs_cursor_mode()
            focus_line = Text(f"Focus: {self.focus} | {self.jobs.summary()}", style="cyan bold")
            status_bar.update(
                Group(
                    self._render_task_panel(),
                    self._render_command_bar(self._context_actions(jobs_view)),
                )
            )
            if self._resume_mask_active():
                header.update(Group(title, focus_line, self._section_header("Please Wait"), Text("Restoring dashboard layout...", style="bold white on black")))
                try:
                    projects_table.styles.display = "none"
                except Exception:
                    pass
                project_summary.update(Text(""))
                try:
                    project_summary.styles.display = "none"
                except Exception:
                    pass
                jobs_header.update(Text(""))
                try:
                    jobs_header.styles.display = "none"
                except Exception:
                    pass
                try:
                    jobs_table_view.clear()
                except Exception:
                    pass
                try:
                    jobs_table_view.styles.display = "none"
                except Exception:
                    pass
                try:
                    footer.styles.display = "none"
                except Exception:
                    pass
                footer.update(Text(""))
                return

            if self.show_help:
                help_text = Text(
                    "Keys: Up/Down select | Enter run | b build | s stop | a adapt | d remove | r restart | v detail | n new\n"
                    "      tab/f focus projects/jobs | o output | x cancel job | c clear completed jobs | y export output\n"
                    "      h toggle help | q quit"
                )
                header.update(Group(title, focus_line, self._section_header("Help"), help_text))
                try:
                    projects_table.styles.display = "none"
                except Exception:
                    pass
                project_summary.update(Text(""))
                try:
                    project_summary.styles.display = "none"
                except Exception:
                    pass
                jobs_header.update(Text(""))
                try:
                    jobs_header.styles.display = "none"
                except Exception:
                    pass
                try:
                    jobs_table_view.clear()
                except Exception:
                    pass
                try:
                    jobs_table_view.styles.display = "none"
                except Exception:
                    pass
                try:
                    footer.styles.display = "block"
                except Exception:
                    pass
                footer.update(Text(""))
                return

            if self.show_project_detail:
                import dataclasses
                dirty = (
                    self.detail_draft is not None and self.detail_original is not None
                    and dataclasses.is_dataclass(self.detail_draft)
                    and dataclasses.asdict(self.detail_draft) != dataclasses.asdict(self.detail_original)
                )
                dirty_tag = "  * unsaved" if dirty else ""
                detail_title = Text(
                    f"Project Detail: {self._project_detail_name() or '-'}{dirty_tag}  (v back · w save · u revert)",
                    style="bold",
                )
                header.update(
                    Group(
                        title,
                        focus_line,
                        self._section_header("Project Detail"),
                        detail_title,
                        Text(""),
                        self._render_project_detail(),
                    )
                )
                try:
                    projects_table.styles.display = "none"
                except Exception:
                    pass
                project_summary.update(Text(""))
                try:
                    project_summary.styles.display = "none"
                except Exception:
                    pass
                jobs_header.update(Text(""))
                try:
                    jobs_header.styles.display = "none"
                except Exception:
                    pass
                try:
                    jobs_table_view.clear()
                except Exception:
                    pass
                try:
                    jobs_table_view.styles.display = "none"
                except Exception:
                    pass
                try:
                    footer.styles.display = "none"
                except Exception:
                    pass
                footer.update(Text(""))
                return

            if self.show_job_output and jobs_view:
                selected = jobs_view[self.selected_job]
                log_title = Text(
                    f"Job #{selected.job_id} {selected.action} {selected.project} [{selected.status}] (press o to close)",
                    style="bold",
                )
                lines = self.jobs.output_lines(selected)
                available, _max_scroll = self._sync_output_window(lines)
                window = lines[self.output_scroll:self.output_scroll + available]
                scroll_hint = (
                    f"lines {self.output_scroll + 1}-{self.output_scroll + len(window)} of {len(lines)}"
                    if lines else "no output"
                )
                log_text = Text("\n".join(window), style="white")
                header.update(
                    Group(
                        title,
                        focus_line,
                        self._section_header("Job Output"),
                        log_title,
                        Text(scroll_hint, style="dim"),
                        Text(""),
                        log_text,
                    )
                )
                # Output mode should be single-pane: hide table regions entirely.
                try:
                    projects_table.styles.display = "none"
                except Exception:
                    pass
                project_summary.update(Text(""))
                try:
                    project_summary.styles.display = "none"
                except Exception:
                    pass
                jobs_header.update(Text(""))
                try:
                    jobs_header.styles.display = "none"
                except Exception:
                    pass
                try:
                    jobs_table_view.clear()
                except Exception:
                    pass
                try:
                    jobs_table_view.styles.display = "none"
                except Exception:
                    pass
                try:
                    footer.styles.display = "none"
                except Exception:
                    pass
                footer.update(Text(""))
                return

            try:
                projects_table.styles.display = "block"
            except Exception:
                pass
            try:
                project_summary.styles.display = "block"
            except Exception:
                pass
            try:
                jobs_header.styles.display = "block"
            except Exception:
                pass
            try:
                jobs_table_view.styles.display = "block"
            except Exception:
                pass
            try:
                footer.styles.display = "block"
            except Exception:
                pass
            header.update(Group(title, focus_line, self._section_header("Projects")))
            fitted_columns = self._fit_project_columns(self.snapshot.columns)
            sig_cols = tuple((name, width) for name, width in fitted_columns)
            sig_rows = tuple(tuple(str(cell) for cell in row.get("cells", [])) for row in self.snapshot.rows)
            sig = (sig_cols, sig_rows)
            if self._project_table_sig != sig:
                self._rebuild_project_table()
            self._set_project_cursor(self.selected)

            summary = Text()
            for idx, line in enumerate(self.snapshot.summary):
                summary.append(line, style=self._summary_style(line))
                if idx < len(self.snapshot.summary) - 1:
                    summary.append("\n")
            project_summary.update(Group(self._section_header("Project Summary"), summary))
            jobs_header.update(self._section_header("Jobs"))

            jobs_sig = tuple(
                (
                    str(job.job_id),
                    job.action,
                    job.project,
                    job.status,
                    _format_age(job.started_at),
                    "-" if job.return_code is None else str(job.return_code),
                )
                for job in jobs_view[:10]
            )
            if self._jobs_table_sig != (("JOBS", "ACTION", "PROJECT", "STATUS", "AGE", "EXIT"), jobs_sig):
                self._rebuild_jobs_table(jobs_view)
            self._set_jobs_cursor(self.selected_job)
            footer.update(Text(""))

        def _render_header_line(self, left: str, right: str) -> Text:
            width = max(20, (getattr(self, "size", None).width or 80))
            spacer = "  "
            raw = f"{left}{spacer}{right}"
            if len(raw) >= width:
                return Text(raw[: max(0, width - 1)], style="bold")
            pad = " " * (width - len(left) - len(right))
            return Text(f"{left}{pad}{right}", style="bold")

        def _section_header(self, label: str) -> Text:
            width = max(20, (getattr(self, "size", None).width or 80))
            prefix = f"── {label} "
            if len(prefix) >= width:
                return Text(prefix[: max(0, width - 1)], style="bold white")
            return Text(prefix + ("─" * (width - len(prefix))), style="bold white")

        def _render_task_panel(self):
            width = max(20, (getattr(self, "size", None).width or 80))
            if self.task_mode:
                if self.task_mode == "job_input":
                    job = next((j for j in self.jobs.jobs if j.job_id == self.task_job_id), None)
                    prompt = job.prompt_text if job and job.prompt_text else f"job #{self.task_job_id} input"
                    prefix = f"{prompt} "
                    suffix = f"{self.task_input}|"
                    full = prefix + suffix
                    if len(full) <= width:
                        return Text(full.ljust(width), style="bold black on white")
                    keep = max(1, width - len(prefix) - 1)
                    scrolled = "…" + suffix[-keep:]
                    line = prefix + scrolled
                    if len(line) > width:
                        line = line[:width]
                    return Text(line.ljust(width), style="bold black on white")
                if self.task_mode == "export_choice":
                    prefix = "export output:"
                    line = Text(prefix.ljust(width), style="bold black on white")
                    options = self.task_export_options or ["Save to file"]
                    rendered = Text(style="bold black on white")
                    for i, option in enumerate(options):
                        if i > 0:
                            rendered.append("  |  ", style="black on white")
                        if i == self.task_option_index:
                            rendered.append(f"[{option}]", style="bold white on blue")
                        else:
                            rendered.append(option, style="bold black on white")
                    plain = rendered.plain
                    if len(plain) > width:
                        rendered = Text(plain[: max(0, width - 1)], style="bold black on white")
                    elif len(plain) < width:
                        rendered.append(" " * (width - len(plain)), style="black on white")
                    return Group(line, rendered)
                if self.task_mode == "adapt_discover":
                    prefix = f"no pending changes for '{self.task_adapt_project}':"
                    line = Text(prefix[:width].ljust(width), style="bold black on white")
                    options = self.task_adapt_options or ["Discover adaptations (--discover)", "Cancel"]
                    rendered = Text(style="bold black on white")
                    for i, option in enumerate(options):
                        if i > 0:
                            rendered.append("  |  ", style="black on white")
                        if i == self.task_option_index:
                            rendered.append(f"[{option}]", style="bold white on blue")
                        else:
                            rendered.append(option, style="bold black on white")
                    plain = rendered.plain
                    if len(plain) > width:
                        rendered = Text(plain[: max(0, width - 1)], style="bold black on white")
                    elif len(plain) < width:
                        rendered.append(" " * (width - len(plain)), style="black on white")
                    return Group(line, rendered)
                if self.task_mode == "project_ssh_add":
                    prefix = f"add SSH key to '{self.task_project_name}':"
                    line = Text(prefix[:width].ljust(width), style="bold black on white")
                    options = self.task_project_options or ["Manual entry...", "Cancel"]
                    rendered = Text(style="bold black on white")
                    for i, option in enumerate(options):
                        if i > 0:
                            rendered.append("  |  ", style="black on white")
                        if i == self.task_option_index:
                            rendered.append(f"[{option}]", style="bold white on blue")
                        else:
                            rendered.append(option, style="bold black on white")
                    plain = rendered.plain
                    if len(plain) > width:
                        rendered = Text(plain[: max(0, width - 1)], style="bold black on white")
                    elif len(plain) < width:
                        rendered.append(" " * (width - len(plain)), style="black on white")
                    return Group(line, rendered)
                if self.task_mode == "project_ssh_remove":
                    prefix = f"remove SSH key from '{self.task_project_name}':"
                    line = Text(prefix[:width].ljust(width), style="bold black on white")
                    options = self.task_project_options or ["Cancel"]
                    rendered = Text(style="bold black on white")
                    for i, option in enumerate(options):
                        if i > 0:
                            rendered.append("  |  ", style="black on white")
                        if i == self.task_option_index:
                            rendered.append(f"[{option}]", style="bold white on blue")
                        else:
                            rendered.append(option, style="bold black on white")
                    plain = rendered.plain
                    if len(plain) > width:
                        rendered = Text(plain[: max(0, width - 1)], style="bold black on white")
                    elif len(plain) < width:
                        rendered.append(" " * (width - len(plain)), style="black on white")
                    return Group(line, rendered)
                if self.task_mode == "project_ssh_add_manual":
                    prefix = f"SSH private key path for '{self.task_project_name}': "
                    suffix = f"{self.task_input}|"
                    full = prefix + suffix
                    if len(full) <= width:
                        return Text(full.ljust(width), style="bold black on white")
                    keep = max(1, width - len(prefix) - 1)
                    scrolled = "…" + suffix[-keep:]
                    line = prefix + scrolled
                    if len(line) > width:
                        line = line[:width]
                    return Text(line.ljust(width), style="bold black on white")
                if self.task_mode == "detail_edit_text":
                    label = self.task_values.get("field_label", "value")
                    prefix = f"edit {label}: "
                    suffix = f"{self.task_input}|"
                    full = prefix + suffix
                    if len(full) <= width:
                        return Text(full.ljust(width), style="bold black on white")
                    keep = max(1, width - len(prefix) - 1)
                    scrolled = "…" + suffix[-keep:]
                    line = (prefix + scrolled)[:width]
                    return Text(line.ljust(width), style="bold black on white")
                if self.task_mode in ("detail_edit_select", "detail_source_action",
                                      "detail_ssh_action", "detail_revert"):
                    label_map = {
                        "detail_edit_select": f"select {self.task_values.get('field_label', 'value')}:",
                        "detail_source_action": "source action:",
                        "detail_ssh_action": "SSH key action:",
                        "detail_revert": "revert to checkpoint:",
                    }
                    prefix = label_map.get(self.task_mode, "select:")
                    options = self.task_project_options or []
                    return self._render_option_panel(prefix, options, self.task_option_index, width)
                if self.task_mode == "detail_source_add":
                    step = self._detail_source_add_current_step()
                    steps = self._detail_source_add_steps()
                    if step is None:
                        return Text("add source".ljust(width), style="bold black on white")
                    idx = self.task_step + 1
                    total = len(steps)
                    if step["kind"] == "text":
                        prefix = f"add source [{idx}/{total}] {step['label']}: "
                        suffix = f"{self.task_input}|"
                        show_err = bool(self.task_error)
                        full = prefix + suffix + (f"  ({self.task_error})" if show_err else "")
                        if len(full) <= width and not show_err:
                            return Text(full.ljust(width), style="bold black on white")
                        if show_err:
                            t = Text(prefix, style="bold black on white")
                            t.append(self.task_input + "|", style="bold black on white")
                            t.append(f"  ({self.task_error})", style="bold red on white")
                            if len(t.plain) < width:
                                t.append(" " * (width - len(t.plain)), style="bold black on white")
                            return t
                        keep = max(1, width - len(prefix) - 1)
                        scrolled = "…" + suffix[-keep:]
                        return Text((prefix + scrolled)[:width].ljust(width), style="bold black on white")
                    prompt = f"add source [{idx}/{total}] {step['label']}:"[:width - 1]
                    options = step.get("options", [])
                    lines = [Text(prompt.ljust(width), style="bold black on white")]
                    for i, opt in enumerate(options):
                        pfx = "> " if i == self.task_option_index else "  "
                        ln = (pfx + opt)[:width - 1]
                        style = "bold white on blue" if i == self.task_option_index else "bold black on white"
                        lines.append(Text(ln.ljust(width), style=style))
                    return Group(*lines)
                step = self._current_task_step()
                steps = self._task_steps()
                if step is None:
                    text = "new project wizard"
                    return Text(text.ljust(width), style="bold black on white")
                idx = self.task_step + 1
                total = len(steps)
                if step["kind"] == "text":
                    prefix = f"new project [{idx}/{total}] {step['label']}: "
                    suffix = f"{self.task_input}|"
                    show_task_error = bool(self.task_error)
                    if show_task_error:
                        suffix = f"{suffix}  ({self.task_error})"
                    full = prefix + suffix
                    if len(full) <= width:
                        if show_task_error:
                            text = Text(prefix, style="bold black on white")
                            text.append(self.task_input + "|", style="bold black on white")
                            text.append("  ", style="bold black on white")
                            text.append(f"({self.task_error})", style="bold red on white")
                            if len(text.plain) < width:
                                text.append(" " * (width - len(text.plain)), style="bold black on white")
                            return text
                        return Text(full.ljust(width), style="bold black on white")
                    # Keep the cursor visible by scrolling horizontally with input growth.
                    keep = max(1, width - len(prefix) - 1)
                    scrolled = "…" + suffix[-keep:]
                    line = (prefix + scrolled)
                    if len(line) > width:
                        line = line[:width]
                    return Text(line.ljust(width), style="bold black on white")
                prompt = f"new project [{idx}/{total}] {step['label']}:"
                if len(prompt) >= width:
                    prompt = prompt[: max(0, width - 1)]
                options = step.get("options", [])
                selected = self.task_option_index
                lines = [Text(prompt.ljust(width), style="bold black on white")]
                for i, option in enumerate(options):
                    prefix = "> " if i == selected else "  "
                    line = prefix + option
                    if len(line) >= width:
                        line = line[: max(0, width - 1)]
                    style = "bold white on blue" if i == selected else "bold black on white"
                    lines.append(Text(line.ljust(width), style=style))
                return Group(*lines)
            msg = self.message.strip() if self.message else "(idle)"
            text = msg
            if len(text) >= width:
                text = text[: max(0, width - 1)]
            return Text(text.ljust(width), style="bold black on white")

        def _render_option_panel(self, prefix: str, options: list, selected: int, width: int):
            """Render a generic option picker in the task panel."""
            line = Text(prefix[:width].ljust(width), style="bold black on white")
            rendered = Text(style="bold black on white")
            for i, option in enumerate(options):
                if i > 0:
                    rendered.append("  |  ", style="black on white")
                if i == selected:
                    rendered.append(f"[{option}]", style="bold white on blue")
                else:
                    rendered.append(option, style="bold black on white")
            plain = rendered.plain
            if len(plain) > width:
                rendered = Text(plain[:max(0, width - 1)], style="bold black on white")
            elif len(plain) < width:
                rendered.append(" " * (width - len(plain)), style="black on white")
            return Group(line, rendered)

        def _context_actions(self, jobs_view: list[DashboardJob]) -> list[tuple[str, str, str]]:
            if self.task_mode:
                if self.task_mode == "job_input":
                    return [
                        ("type", "Reply", "bold cyan"),
                        ("⏎", "Send Input", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode == "export_choice":
                    return [
                        ("←/→", "Choose", "bold yellow"),
                        ("⏎", "Export", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode == "adapt_discover":
                    return [
                        ("←/→", "Choose", "bold yellow"),
                        ("⏎", "Confirm", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode in {"project_ssh_add", "project_ssh_remove"}:
                    return [
                        ("←/→", "Choose", "bold yellow"),
                        ("⏎", "Confirm", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode == "project_ssh_add_manual":
                    return [
                        ("type", "Edit Text", "bold cyan"),
                        ("⏎", "Save", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode == "detail_edit_text":
                    return [
                        ("type", "Edit", "bold cyan"),
                        ("⏎", "Apply", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode in ("detail_edit_select", "detail_source_action",
                                      "detail_ssh_action", "detail_revert"):
                    return [
                        ("↑/↓", "Choose", "bold yellow"),
                        ("⏎", "Apply", "bold green"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                if self.task_mode == "detail_source_add":
                    step = self._detail_source_add_current_step()
                    if step and step["kind"] == "text":
                        return [
                            ("type", "Edit", "bold cyan"),
                            ("⏎", "Next", "bold green"),
                            ("Del", "Back", "bold yellow"),
                            ("Esc", "Cancel", "bold red"),
                        ]
                    return [
                        ("↑/↓", "Choose", "bold yellow"),
                        ("⏎", "Next", "bold green"),
                        ("Del", "Back", "bold yellow"),
                        ("Esc", "Cancel", "bold red"),
                    ]
                return [
                    ("←/→", "Select", "bold yellow"),
                    ("type", "Edit Text", "bold cyan"),
                    ("⏎", "Next", "bold green"),
                    ("Del", "Back", "bold yellow"),
                    ("Esc", "Cancel", "bold red"),
                ]
            nav = [
                ("↑/↓", "Move", "bold white"),
            ]
            if self.show_job_output:
                return [
                    ("↑/↓", "Scroll Output", "bold white"),
                    ("←/→", "Switch Job", "bold yellow"),
                    ("o", "Close Output", "bold cyan"),
                    ("x", "Cancel", "bold red"),
                    ("d", "Remove Job", "bold red"),
                    ("y", "Export Output", "bold blue"),
                    ("q", "Quit", "bold bright_black"),
                ]
            if self.focus == "jobs":
                if jobs_view:
                    return nav + [
                        ("o", "Output", "bold cyan"),
                        ("x", "Cancel", "bold red"),
                        ("d", "Remove Job", "bold red"),
                        ("c", "Clear Done", "bold yellow"),
                        ("y", "Export", "bold blue"),
                        ("q", "Quit", "bold bright_black"),
                    ]
                return [
                    ("q", "Quit", "bold bright_black"),
                ]
            if self.show_project_detail:
                import dataclasses
                dirty = False
                if (self.detail_draft is not None and self.detail_original is not None
                        and dataclasses.is_dataclass(self.detail_draft)):
                    d_d = dataclasses.asdict(self.detail_draft)
                    o_d = dataclasses.asdict(self.detail_original)
                    for d in (d_d, o_d):
                        d.pop("state", None)
                        d.pop("resources", None)
                    dirty = d_d != o_d
                has_checkpoints = bool(self.detail_checkpoints)
                actions = [
                    ("↑/↓", "Navigate", "bold white"),
                    ("⏎", "Edit field", "bold green"),
                    ("w", "Save *" if dirty else "Save", "bold yellow" if dirty else "bold white"),
                    ("v", "Back", "bold cyan"),
                ]
                if has_checkpoints:
                    actions.append(("u", "Revert", "bold magenta"))
                actions += [
                    ("b", "Build", "bold yellow"),
                    ("s", "Stop", "bold yellow"),
                    ("q", "Quit", "bold bright_black"),
                ]
                return actions
            return nav + [
                ("⏎", "Run", "bold green"),
                ("b", "Build", "bold yellow"),
                ("s", "Stop", "bold yellow"),
                ("a", "Adapt", "bold cyan"),
                ("d", "Remove", "bold red"),
                ("r", "Restart", "bold blue"),
                ("v", "Detail", "bold cyan"),
                ("n", "New", "bold green"),
                ("q", "Quit", "bold bright_black"),
            ]

        def _render_command_bar(self, actions: list[tuple[str, str, str]]) -> Text:
            width = max(20, (getattr(self, "size", None).width or 80))
            text = Text(style="white on black")
            for idx, (key, label, style) in enumerate(actions):
                if idx > 0:
                    text.append("  ·  ", style="bold white on black")
                text.append(f"{key} ", style=f"{style} on black")
                text.append(label, style="bold white on black")
            content = text.plain
            if len(content) > width:
                clipped = content[: max(0, width - 1)]
                return Text(clipped, style="bold white on black")
            if len(content) < width:
                text.append(" " * (width - len(content)), style="white on black")
            return text

        def _render_jobs_table(self, jobs_view: list[DashboardJob]):
            table = Table(box=None, show_edge=False, pad_edge=False)
            table.add_column("JOBS", width=6)
            table.add_column("ACTION", width=8)
            table.add_column("PROJECT", width=18, overflow="ellipsis", no_wrap=True)
            table.add_column("STATUS", width=14)
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

        def _render_project_detail(self):
            project_name = self._project_detail_name()
            if not project_name:
                return Text("No project selected.", style="dim")
            draft = self.detail_draft
            if draft is None:
                return Text("Loading…", style="dim")
            store = ConfigStore()
            fields = _project_detail_fields(draft, store)
            # Determine unsaved state for header marker
            import dataclasses
            dirty = False
            if self.detail_original is not None and dataclasses.is_dataclass(draft):
                d_d = dataclasses.asdict(draft)
                o_d = dataclasses.asdict(self.detail_original)
                for d in (d_d, o_d):
                    d.pop("state", None)
                    d.pop("resources", None)
                dirty = d_d != o_d
            checkpoint_count = len(self.detail_checkpoints)
            text = Text()
            for idx, f in enumerate(fields):
                display = f["display"]
                is_cursor = (not self.task_mode) and f.get("editable") and idx == self.detail_cursor
                if f.get("section"):
                    if display.startswith("Project:") and dirty:
                        display = display + "  *"
                    style = "bold white"
                elif is_cursor:
                    style = "bold white on blue"
                elif f.get("editable"):
                    if f.get("action") in ("detail_save", "detail_revert"):
                        if f["action"] == "detail_revert" and not (dirty or checkpoint_count):
                            style = "dim"
                        else:
                            style = "bold cyan"
                    else:
                        style = "bold"
                else:
                    style = ""
                prefix = "> " if is_cursor else "  " if f.get("editable") and not f.get("section") and not display.startswith("  ") else ""
                if is_cursor and not display.startswith("  "):
                    prefix = "> "
                elif is_cursor:
                    display = "> " + display[2:]  # replace leading spaces with cursor
                    prefix = ""
                text.append(display if prefix == "" else prefix + display, style=style)
                if idx < len(fields) - 1:
                    text.append("\n")
            # Checkpoint summary line
            if checkpoint_count:
                text.append(f"\n  ({checkpoint_count} checkpoint{'s' if checkpoint_count != 1 else ''} available for revert)", style="dim")
            return text

        def _fit_project_columns(self, columns: list[tuple[str, int]]) -> list[tuple[str, int]]:
            """Clamp project column widths to viewport width to avoid row wrapping."""
            if not columns:
                return []
            width = max(20, (getattr(self, "size", None).width or 80))
            budget = max(10, width - 1)  # keep one char clear to avoid edge wraps
            budget -= max(0, len(columns) - 1)  # rough inter-column spacing
            budget = max(6, budget)

            fitted = [(name, max(1, int(col_w))) for name, col_w in columns]
            total = sum(col_w for _, col_w in fitted)
            if total <= budget:
                return fitted

            min_widths = {
                "NAME": 8,
                "ACTIVITY": 8,
                "STATUS": 8,
                "HOST": 8,
                "SOURCE": 14,
                "GIT": 6,
                "IMAGE": 14,
                "RUNNING-IMAGE": 14,
                "AGENT": 6,
                "CREDENTIAL": 10,
                "SECURITY": 8,
                "NETWORK": 6,
            }
            shrink_order = [
                "SOURCE",
                "RUNNING-IMAGE",
                "IMAGE",
                "CREDENTIAL",
                "NAME",
                "ACTIVITY",
                "STATUS",
                "HOST",
                "AGENT",
                "SECURITY",
                "NETWORK",
                "GIT",
            ]
            idx_by_name = {name: idx for idx, (name, _) in enumerate(fitted)}
            while total > budget:
                changed = False
                for name in shrink_order:
                    idx = idx_by_name.get(name)
                    if idx is None:
                        continue
                    cur = fitted[idx][1]
                    floor = min_widths.get(name, 4)
                    if cur <= floor:
                        continue
                    fitted[idx] = (name, cur - 1)
                    total -= 1
                    changed = True
                    if total <= budget:
                        break
                if not changed:
                    break
            return fitted

        @staticmethod
        def _job_status_style(status: str) -> str:
            if status in ("running", "queued"):
                return "yellow bold"
            if status in ("waiting_input",):
                return "magenta bold"
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
                agent = value.strip().lower()
                if agent == "claude" or "anthropic" in agent:
                    return "#D97706 bold"
                if agent == "codex" or "openai" in agent or "gpt" in agent:
                    return "#10A37F bold"
                if agent == "gemini" or "google" in agent:
                    return "#4285F4 bold"
                return "cyan bold"
            if column == "CREDENTIAL":
                if value == "(none)":
                    return "dim"
                if "!" in value:
                    return "yellow bold"
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
            self._log_ui_event("action", name="toggle_help")
            self.show_help = not self.show_help
            self._refresh_view()

        def action_toggle_project_detail(self) -> None:
            self._log_ui_event("action", name="toggle_project_detail")
            self._toggle_project_detail()

        def action_quit(self) -> None:
            active_jobs = _active_jobs_for_quit(self.jobs.jobs)
            if not active_jobs:
                self._log_ui_event("action", name="quit")
                self.exit()
                return

            now = time.monotonic()
            if now < float(self._quit_armed_until):
                self._log_ui_event("action", name="quit_confirmed", active_jobs=len(active_jobs))
                self.exit()
                return

            self._quit_armed_until = now + 3.0
            self._log_ui_event("action", name="quit_blocked", active_jobs=len(active_jobs))
            self.message = (
                f"warning: {len(active_jobs)} active job(s) would be orphaned; "
                "press q again within 3s to quit anyway"
            )
            self._refresh_view()

        def action_toggle_focus(self) -> None:
            self._log_ui_event("action", name="toggle_focus")
            if self.task_mode:
                self.focus = "task"
                self._refresh_view()
                return
            if self.show_project_detail:
                self.focus = "projects"
                self._refresh_view()
                return
            if self.show_job_output:
                # Job output mode is jobs-only; keep focus stable.
                self.focus = "jobs"
                self._refresh_view()
                return
            self.focus = "jobs" if self.focus == "projects" else "projects"
            self.show_job_output = False
            self._refresh_view()

        def _nav_context(self) -> tuple[list[DashboardJob], int, int]:
            jobs_view = self.jobs.list_for_view()
            project_count = len(self.snapshot.rows)
            visible_jobs = min(len(jobs_view), 10)
            if visible_jobs > 0:
                self.selected_job = max(0, min(self.selected_job, visible_jobs - 1))
            else:
                self.selected_job = 0
            return jobs_view, project_count, visible_jobs

        def _job_output_available_lines(self) -> int:
            viewport_h = (getattr(self, "size", None).height or 24)
            # Reserve non-output rows (header/focus/section/title/hint/spacer/status bar).
            return max(5, viewport_h - 11)

        def _jump_output_to_end(self) -> None:
            # Use a large scroll sentinel; render-time clamping will pin to tail.
            self.output_scroll = 10**9
            self.output_follow = True

        def _sync_output_window(self, lines: list[str]) -> tuple[int, int]:
            available = self._job_output_available_lines()
            max_scroll = max(0, len(lines) - available)
            if self.output_follow:
                self.output_scroll = max_scroll
            else:
                self.output_scroll = max(0, min(self.output_scroll, max_scroll))
                if self.output_scroll >= max_scroll:
                    self.output_follow = True
            return available, max_scroll

        def _move_jobs_focus(self, delta: int, jobs_view: list[DashboardJob], project_count: int, visible_jobs: int) -> str:
            if not jobs_view and project_count:
                self.focus = "projects"
                self._set_selected_project_index(self.selected)
                return "refresh"
            if not jobs_view:
                return "none"

            if delta < 0:
                if self.selected_job > 0:
                    self.selected_job -= 1
                    if self._use_project_widget and self.focus == "jobs":
                        self._refresh_jobs_widget_local()
                        return "local"
                elif project_count:
                    self.focus = "projects"
                    self._set_selected_project_index(project_count - 1)
                return "refresh"

            if self.selected_job < visible_jobs - 1:
                self.selected_job += 1
                if self._use_project_widget and self.focus == "jobs":
                    self._refresh_jobs_widget_local()
                    return "local"
            elif project_count:
                self.focus = "projects"
                self._set_selected_project_index(0)
            return "refresh"

        def _move_projects_focus(self, delta: int, jobs_view: list[DashboardJob], project_count: int, visible_jobs: int) -> str:
            if not project_count and jobs_view:
                self.focus = "jobs"
                self.selected_job = max(0, visible_jobs - 1) if delta < 0 else 0
                return "refresh"
            if not project_count:
                return "none"

            if delta < 0:
                if self.selected > 0:
                    self._set_selected_project_index(self.selected - 1)
                    if self._use_project_widget and self.focus == "projects":
                        self._set_project_cursor(self.selected)
                        return "local"
                elif jobs_view:
                    self.focus = "jobs"
                    self.selected_job = max(0, visible_jobs - 1)
                return "refresh"

            if self.selected < project_count - 1:
                self._set_selected_project_index(self.selected + 1)
                if self._use_project_widget and self.focus == "projects":
                    self._set_project_cursor(self.selected)
                    return "local"
            elif jobs_view:
                self.focus = "jobs"
                self.selected_job = 0
            return "refresh"

        def _move_cursor(self, delta: int) -> None:
            jobs_view, project_count, visible_jobs = self._nav_context()
            if self.show_job_output:
                step = 5
                if delta < 0:
                    self.output_follow = False
                    self.output_scroll = max(0, self.output_scroll - step)
                elif jobs_view:
                    selected = jobs_view[self.selected_job]
                    lines = self.jobs.output_lines(selected)
                    _available, max_scroll = self._sync_output_window(lines)
                    self.output_scroll = min(max_scroll, self.output_scroll + step)
                    if self.output_scroll >= max_scroll:
                        self.output_follow = True
                self.focus = "jobs"
                self._refresh_view()
                return

            if self.focus == "jobs":
                outcome = self._move_jobs_focus(delta, jobs_view, project_count, visible_jobs)
            else:
                outcome = self._move_projects_focus(delta, jobs_view, project_count, visible_jobs)

            if outcome == "refresh":
                self._refresh_view()

        def action_cursor_up(self) -> None:
            self._log_ui_event("action", name="cursor_up")
            if self.task_mode:
                if self.task_mode == "job_input":
                    return
                step = self._current_task_step()
                if self.task_mode in _PROJECT_DETAIL_TASKS:
                    self._task_shift_option(-1)
                    return
                if step is not None and step["kind"] == "select":
                    self._task_shift_option(-1)
                return
            if self.show_project_detail:
                self._move_detail_cursor(-1)
                return
            self._move_cursor(-1)

        def action_cursor_down(self) -> None:
            self._log_ui_event("action", name="cursor_down")
            if self.task_mode:
                if self.task_mode == "job_input":
                    return
                step = self._current_task_step()
                if self.task_mode in _PROJECT_DETAIL_TASKS:
                    self._task_shift_option(1)
                    return
                if step is not None and step["kind"] == "select":
                    self._task_shift_option(1)
                return
            if self.show_project_detail:
                self._move_detail_cursor(1)
                return
            self._move_cursor(1)

        def _move_detail_cursor(self, delta: int) -> None:
            if self.detail_draft is None:
                return
            store = ConfigStore()
            fields = _project_detail_fields(self.detail_draft, store)
            editable = [i for i, f in enumerate(fields) if f.get("editable")]
            if not editable:
                return
            cur = self.detail_cursor
            if cur in editable:
                pos = editable.index(cur)
            else:
                pos = 0
            pos = (pos + delta) % len(editable)
            self.detail_cursor = editable[pos]
            self._refresh_view()

        def _run_selected(self, action_key: str) -> None:
            if self.task_mode:
                self._task_submit_step()
                return
            if self.show_project_detail and action_key == "run":
                # In detail view, Enter activates the field at the cursor
                self._activate_detail_field()
                return
            if self.show_job_output or self.focus != "projects":
                self.message = "project actions are available only in projects view"
                self._refresh_view()
                return
            project_name = self._selected_project_name()
            if not project_name:
                return
            lock_msg = _lock_block_message(project_name, action_key)
            if lock_msg:
                self.message = lock_msg
                self._refresh_view()
                return
            if action_key == "adapt":
                try:
                    pending_adapt = self._project_has_pending_adapt(project_name)
                except Exception:
                    pending_adapt = True
                if not pending_adapt:
                    self._start_adapt_discover_task(project_name)
                    self._refresh_view()
                    return
            if action_key == "run":
                checks: list[BuildPreflightCheck] = []
                errors: list[str] = []
                if not self._project_is_running(project_name):
                    try:
                        checks, errors = _run_preflight_checks(project_name)
                    except Exception as exc:
                        self.message = f"run preflight failed for '{project_name}': {type(exc).__name__}: {exc}"
                        self._refresh_view()
                        return
                if checks:
                    queued = []
                    failures = []
                    for check in checks:
                        lock_msg = _lock_block_message(check.project, "build")
                        if lock_msg:
                            failures.append(lock_msg)
                            continue
                        background = _background_command("build", check.project)
                        if not background:
                            failures.append(f"build action is unavailable for project '{check.project}'")
                            continue
                        try:
                            job = self.jobs.enqueue("build", check.project, command=background)
                            queued.append((check.project, check.reason, job.job_id))
                        except Exception as exc:
                            failures.append(
                                f"failed to queue build for '{check.project}': {type(exc).__name__}: {exc}"
                            )

                    if queued:
                        queued_names = ", ".join(
                            f"{name}#{job_id}" for name, _reason, job_id in queued
                        )
                        self.message = (
                            f"queued preflight build(s): {queued_names}. "
                            f"Run '{project_name}' again after the build job(s) complete."
                        )
                    else:
                        self.message = f"run preflight blocked for '{project_name}': no build job could be queued."
                    if errors or failures:
                        detail = errors + failures
                        self.message = f"{self.message} ({detail[0]})"
                    self.show_job_output = False
                    self.selected_job = 0
                    self._refresh_view()
                    self._request_refresh()
                    return
                if errors:
                    self.message = f"run preflight warning: {errors[0]}"
                    self._refresh_view()
            background = _background_command(action_key, project_name)
            if background is not None:
                try:
                    job = self.jobs.enqueue(action_key, project_name, command=background)
                    self.message = f"queued job #{job.job_id}: {action_key} {project_name}"
                    self.show_job_output = False
                    self.selected_job = 0
                except Exception as exc:
                    self.message = f"failed to queue {action_key} {project_name}: {type(exc).__name__}: {exc}"
            else:
                # In Emacs inline mode, keep dashboard and attached terminal from
                # competing for input by handing off the process for interactive
                # attach actions.
                interactive_replace = bool(inside_emacs and not use_screen and action_key in {"run", "restart"})
                self.message = _run_action_interactive(
                    action_key,
                    project_name,
                    suspend=self.suspend,
                    replace_process=interactive_replace,
                )
                if self._use_project_widget:
                    # Returning from suspend can briefly report a transiently
                    # narrow viewport. Defer redraw so column fitting uses the
                    # settled terminal size.
                    self._project_table_sig = None
                    self._jobs_table_sig = None
                    self._begin_resume_mask(1.0)
                else:
                    self.set_timer(0.1, self._refresh_view)
                    self.set_timer(0.1, self._request_refresh)
                self._refresh_view()
                return
            self._refresh_view()
            self._request_refresh()

        def action_run_selected(self) -> None:
            self._log_ui_event("action", name="run_selected")
            if self.focus == "jobs":
                self.action_open_job_output()
                return
            self._run_selected("run")

        def action_build_selected(self) -> None:
            self._log_ui_event("action", name="build_selected")
            self._run_selected("build")

        def action_stop_selected(self) -> None:
            self._log_ui_event("action", name="stop_selected")
            self._run_selected("stop")

        def action_adapt_selected(self) -> None:
            self._log_ui_event("action", name="adapt_selected")
            self._run_selected("adapt")

        def action_remove_selected(self) -> None:
            self._log_ui_event("action", name="remove_selected")
            if self.focus == "jobs" or self.show_job_output:
                self.action_remove_job()
                return
            if self.focus != "projects":
                self.message = "remove is available only in projects view"
                self._refresh_view()
                return
            project_name = self._selected_project_name()
            if not project_name:
                return
            if self._project_is_running(project_name):
                self.message = f"project '{project_name}' is running; stop it before remove"
                self._refresh_view()
                return
            lock_msg = _lock_block_message(project_name, "remove")
            if lock_msg:
                self.message = lock_msg
                self._refresh_view()
                return
            job, error = _enqueue_remove_job(self.jobs, project_name)
            if job is None:
                self.message = error
            else:
                self.message = f"queued job #{job.job_id}: remove {project_name}"
                self.show_job_output = False
                self.selected_job = 0
            self._refresh_view()

        def action_restart_selected(self) -> None:
            self._log_ui_event("action", name="restart_selected")
            self._run_selected("restart")

        def action_add_project_ssh(self) -> None:
            self._log_ui_event("action", name="add_project_ssh")
            if self.task_mode:
                self._refresh_view()
                return
            if not self.show_project_detail:
                self.message = "SSH key add is available only in project detail"
                self._refresh_view()
                return
            project_name = self._project_detail_name()
            if not project_name:
                self.message = "no project selected"
                self._refresh_view()
                return
            self._start_project_ssh_add_task(project_name)
            self._refresh_view()

        def action_new_project(self) -> None:
            self._log_ui_event("action", name="new_project")
            if self.task_mode:
                self._refresh_view()
                return
            if self.show_project_detail:
                self.message = "return to the project list before creating a new project"
                self._refresh_view()
                return
            if self.show_job_output or self.focus not in ("projects", "jobs"):
                self.message = "new project is not available in this view"
                self._refresh_view()
                return
            self.task_catalog = self._build_new_project_catalog()
            self._start_new_project_task()
            self._refresh_view()

        def action_open_job_output(self) -> None:
            self._log_ui_event("action", name="open_job_output")
            if self.task_mode == "new_project":
                self._refresh_view()
                return
            if self.focus != "jobs" and not self.show_job_output:
                self.message = "switch focus to jobs to open output"
                self._refresh_view()
                return
            jobs_view = self.jobs.list_for_view()
            if not jobs_view:
                self.message = "no jobs to display"
                self._refresh_view()
                return
            self.selected_job = min(self.selected_job, len(jobs_view) - 1)
            self.focus = "jobs"
            self.show_job_output = not self.show_job_output
            if self.show_job_output:
                self._jump_output_to_end()
            else:
                self.output_follow = False
            job = jobs_view[self.selected_job]
            if self.show_job_output and job.status == "waiting_input":
                self._start_job_input_task(job)
            elif self.task_mode == "job_input":
                self.task_mode = ""
                self.task_job_id = 0
            self._refresh_view()

        def action_cancel_job(self) -> None:
            self._log_ui_event("action", name="cancel_job")
            if self.show_project_detail and not self.task_mode:
                project_name = self._project_detail_name()
                if not project_name:
                    self.message = "no project selected"
                    self._refresh_view()
                    return
                self._start_project_ssh_remove_task(project_name)
                self._refresh_view()
                return
            if self.focus != "jobs" and not self.show_job_output:
                self.message = "switch focus to jobs to cancel jobs"
                self._refresh_view()
                return
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
            self._log_ui_event("action", name="clear_jobs")
            if self.focus != "jobs" and not self.show_job_output:
                self.message = "switch focus to jobs to clear jobs"
                self._refresh_view()
                return
            removed = self.jobs.clear_completed()
            self.message = f"cleared {removed} completed job(s)"
            self.show_job_output = False
            self.output_follow = False
            self._refresh_view()

        def action_export_job_output(self) -> None:
            self._log_ui_event("action", name="export_job_output")
            if self.task_mode:
                self._refresh_view()
                return
            if self.focus != "jobs" and not self.show_job_output:
                self.message = "switch focus to jobs to export output"
                self._refresh_view()
                return
            jobs_view = self.jobs.list_for_view()
            if not jobs_view:
                self.message = "no jobs to export"
                self._refresh_view()
                return
            self.selected_job = min(self.selected_job, len(jobs_view) - 1)
            job = jobs_view[self.selected_job]
            self._start_export_choice_task(job)
            self._refresh_view()

        def action_remove_job(self) -> None:
            self._log_ui_event("action", name="remove_job")
            if self.task_mode:
                self._refresh_view()
                return
            if self.focus != "jobs" and not self.show_job_output:
                self.message = "switch focus to jobs to remove a job"
                self._refresh_view()
                return
            jobs_view = self.jobs.list_for_view()
            if not jobs_view:
                self.message = "no jobs to remove"
                self._refresh_view()
                return
            self.selected_job = min(self.selected_job, len(jobs_view) - 1)
            job = jobs_view[self.selected_job]
            ok, detail = self.jobs.remove_job(job.job_id, delete_log=False)
            if ok:
                self.message = f"removed job #{job.job_id}"
                remaining = self.jobs.list_for_view()
                if not remaining:
                    self.selected_job = 0
                    self.show_job_output = False
                    self.output_follow = False
                    if self.snapshot.rows:
                        self.focus = "projects"
                else:
                    self.selected_job = min(self.selected_job, len(remaining) - 1)
            else:
                self.message = detail
            self._refresh_view()

        def action_task_prev_option(self) -> None:
            self._log_ui_event("action", name="task_prev_option")
            if self.show_job_output and not self.task_mode:
                jobs_view = self.jobs.list_for_view()
                if jobs_view:
                    self.selected_job = max(0, self.selected_job - 1)
                    self._jump_output_to_end()
                    self._refresh_view()
                return
            if not self.task_mode:
                if self._use_project_widget and self.focus == "projects":
                    self._scroll_table_x("#projects-table", -4)
                elif self._use_project_widget and self.focus == "jobs":
                    self._scroll_table_x("#jobs-table", -4)
                elif self.focus == "projects":
                    next_offset = max(0, self.project_hscroll - 4)
                    if next_offset != self.project_hscroll:
                        self.project_hscroll = next_offset
                        if self._use_project_widget:
                            self._project_table_sig = None
                        self._refresh_view()
                elif self.focus == "jobs":
                    next_offset = max(0, self.jobs_hscroll - 4)
                    if next_offset != self.jobs_hscroll:
                        self.jobs_hscroll = next_offset
                        if self._use_project_widget:
                            self._jobs_table_sig = None
                        self._refresh_view()
                return
            self._task_shift_option(-1)

        def action_task_next_option(self) -> None:
            self._log_ui_event("action", name="task_next_option")
            if self.show_job_output and not self.task_mode:
                jobs_view = self.jobs.list_for_view()
                if jobs_view:
                    self.selected_job = min(min(len(jobs_view), 10) - 1, self.selected_job + 1)
                    self._jump_output_to_end()
                    self._refresh_view()
                return
            if not self.task_mode:
                if self._use_project_widget and self.focus == "projects":
                    self._scroll_table_x("#projects-table", 4)
                elif self._use_project_widget and self.focus == "jobs":
                    self._scroll_table_x("#jobs-table", 4)
                elif self.focus == "projects":
                    limit = self._max_project_hscroll()
                    next_offset = min(limit, self.project_hscroll + 4)
                    if next_offset != self.project_hscroll:
                        self.project_hscroll = next_offset
                        if self._use_project_widget:
                            self._project_table_sig = None
                        self._refresh_view()
                elif self.focus == "jobs":
                    jobs_view = self.jobs.list_for_view()
                    limit = self._max_jobs_hscroll(jobs_view)
                    next_offset = min(limit, self.jobs_hscroll + 4)
                    if next_offset != self.jobs_hscroll:
                        self.jobs_hscroll = next_offset
                        if self._use_project_widget:
                            self._jobs_table_sig = None
                        self._refresh_view()
                return
            self._task_shift_option(1)

        def action_task_cancel(self) -> None:
            self._log_ui_event("action", name="task_cancel")
            if not self.task_mode:
                if self.show_project_detail:
                    self._toggle_project_detail()
                return
            if self.task_mode in _PROJECT_DETAIL_TASKS:
                self._task_cancel(f"edit cancelled")
                self._refresh_view()
                return
            if self.task_mode == "job_input":
                self.task_mode = ""
                self.task_job_id = 0
                self.focus = "jobs"
                self.message = "job input cancelled"
                self._refresh_view()
                return
            if self.task_mode == "adapt_discover":
                self._task_cancel("adapt cancelled")
                self._refresh_view()
                return
            self._task_cancel()
            self._refresh_view()

    _set_terminal_title("<<< skua >>>")

    app = DashboardApp(args)
    try:
        params = set(inspect.signature(app.run).parameters.keys())
    except (TypeError, ValueError):
        params = set()
    has_inline_mode = "inline" in params

    if screen_override in {"1", "true", "yes", "on"}:
        use_screen = True
    elif screen_override in {"0", "false", "no", "off"}:
        use_screen = False
    else:
        # Textual 8+ inline mode is incompatible with suspend() and tends to be
        # unstable in Emacs terminal buffers. Prefer app/screen mode there.
        if inside_emacs and has_inline_mode:
            use_screen = True
        else:
            use_screen = not inside_emacs

    if use_screen:
        run_kwargs = {}
        # Avoid terminal mouse tracking in Emacs terminals.
        if inside_emacs and "mouse" in params:
            run_kwargs["mouse"] = False
        app.run(**run_kwargs)
        return

    # Textual has evolved its run kwargs across versions:
    # - Older API: run(screen=False)
    # - Newer API: run(inline=True, inline_no_clear=True)
    if "screen" in params:
        app.run(screen=False)
        return
    if "inline" in params:
        kwargs = {"inline": True}
        if "inline_no_clear" in params:
            kwargs["inline_no_clear"] = True
        app.run(**kwargs)
        return
    app.run()
