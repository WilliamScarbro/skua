# SPDX-License-Identifier: BUSL-1.1
"""skua dashboard — live interactive project dashboard."""

import threading
from contextlib import nullcontext
from dataclasses import dataclass
from types import SimpleNamespace

from skua.commands.adapt import cmd_adapt
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


@dataclass
class DashboardSnapshot:
    """Rendered table and metadata for one dashboard refresh."""

    columns: list
    rows: list
    summary: list


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
        try:
            input("Press Enter to return to dashboard...")
        except EOFError:
            pass
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
            Binding("up,k", "cursor_up", "Up"),
            Binding("down,j", "cursor_down", "Down"),
            Binding("enter", "run_selected", "Run"),
            Binding("b", "build_selected", "Build"),
            Binding("s", "stop_selected", "Stop"),
            Binding("a", "adapt_selected", "Adapt"),
            Binding("d", "remove_selected", "Remove"),
            Binding("r", "restart_selected", "Restart"),
        ]

        def __init__(self, dashboard_args):
            super().__init__()
            self.dashboard_args = dashboard_args
            self.snapshot = DashboardSnapshot(columns=[], rows=[], summary=[])
            self.selected = 0
            self.show_help = False
            self.message = ""
            self._refresh_lock = threading.Lock()
            self._refresh_inflight = False

        def compose(self) -> ComposeResult:
            yield Static(id="dashboard-view")
            yield Footer()

        def on_mount(self) -> None:
            self._request_refresh()
            self.set_interval(2.0, self._request_refresh)

        def _request_refresh(self) -> None:
            with self._refresh_lock:
                if self._refresh_inflight:
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

            if self.show_help:
                help_text = Text(
                    "Keys: Up/Down select | Enter run | b build | s stop | a adapt | d remove | r restart\n"
                    "      h toggle help | q quit\n\n"
                    "Actions run for the selected project and then return to the dashboard."
                )
                view.update(Group(title, message, help_text))
                return

            if not self.snapshot.rows:
                summary = Text("\n".join(self.snapshot.summary + ["Press q to quit. Press h for help."]))
                view.update(Group(title, message, summary))
                return

            table = Table(box=None, show_edge=False, pad_edge=False)
            for col_name, col_width in self.snapshot.columns:
                table.add_column(col_name, width=col_width, overflow="ellipsis", no_wrap=True)
            for idx, row in enumerate(self.snapshot.rows):
                style = "reverse" if idx == self.selected else ""
                table.add_row(*[str(cell) for cell in row["cells"]], style=style)

            summary = Text("\n".join(self.snapshot.summary + ["Press h for help. Press q to quit."]))
            view.update(Group(title, message, table, summary))

        def action_toggle_help(self) -> None:
            self.show_help = not self.show_help
            self._refresh_view()

        def action_cursor_up(self) -> None:
            if self.snapshot.rows:
                self.selected = max(0, self.selected - 1)
                self._refresh_view()

        def action_cursor_down(self) -> None:
            if self.snapshot.rows:
                self.selected = min(len(self.snapshot.rows) - 1, self.selected + 1)
                self._refresh_view()

        def _run_selected(self, action_key: str) -> None:
            project_name = self._selected_project_name()
            if not project_name:
                return
            self.message = _run_action_interactive(action_key, project_name, suspend=self.suspend)
            self._refresh_view()
            self._request_refresh()

        def action_run_selected(self) -> None:
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

    DashboardApp(args).run()
