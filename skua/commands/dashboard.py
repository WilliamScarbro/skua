# SPDX-License-Identifier: BUSL-1.1
"""skua dashboard — live interactive project dashboard."""

import curses
from dataclasses import dataclass
from types import SimpleNamespace

from skua.commands.adapt import cmd_adapt
from skua.commands.list_cmd import (
    _agent_activity,
    _container_image_id,
    _container_image_name,
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


class DashboardColors:
    """Color pair IDs for dashboard rendering."""

    HEADER = 1
    NAME = 2
    STATUS_GOOD = 3
    STATUS_WARN = 4
    STATUS_BAD = 5
    GIT_GOOD = 6
    GIT_WARN = 7
    GIT_BAD = 8
    ACTIVITY_BUSY = 9
    ACTIVITY_IDLE = 10
    ACTIVITY_DONE = 11
    ACTIVITY_BAD = 12
    SOURCE = 13
    HOST = 14
    IMAGE_WARN = 15
    AGENT = 16


def _collect_snapshot(args) -> DashboardSnapshot:
    store = ConfigStore()
    project_names = store.list_resources("Project")
    running_by_host = {"": set(get_running_skua_containers())}
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
            running_by_host[normalized] = set(get_running_skua_containers(host=normalized))
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

    columns = [("NAME", 16)]
    columns.append(("ACTIVITY", 14))
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
    columns.append(("STATUS", 10))

    pending_count = 0
    running_count = 0
    needs_adapt = False
    needs_build = False
    activity_values = {}
    for name, project in projects:
        container_name = f"skua-{name}"
        host = getattr(project, "host", "") or ""
        if container_name in _running_for_host(host):
            activity_values[name] = _agent_activity(container_name, host=host)
        else:
            activity_values[name] = "-"

    rows = []
    for name, project in projects:
        container_name = f"skua-{name}"
        host = getattr(project, "host", "") or ""
        running = _running_for_host(host)
        pending_adapt = _has_pending_adapt_request(project)
        img_name = image_name_for_project(image_name_base, project)
        suffix, flags = _image_suffix(project, store)
        stale_adapt = "(A)" in flags
        stale_build = "(B)" in flags
        if stale_adapt:
            needs_adapt = True
        if stale_build:
            needs_build = True

        if container_name in running:
            status = "running"
            running_count += 1
        else:
            status = "built" if image_exists(img_name) else "missing"

        if stale_adapt or stale_build:
            if status == "built":
                status = "stale"
            elif status == "running":
                status = "running!"

        if pending_adapt:
            status += "*"
            pending_count += 1

        row = [name]
        row.append(activity_values.get(name, "-"))
        if show_host:
            row.append(_format_host(project))
        row.append(_format_source(project))
        if show_git:
            row.append(_git_status(project, store) or "-")
        if show_image:
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
        row.append(status)
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


def _render_cells(columns: list, cells: list) -> str:
    return " ".join(f"{str(value):<{width}}" for (title, width), value in zip(columns, cells))


def _clip_line(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width == 1:
        return text[:1]
    return text[: width - 1] + ">"


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
        cmd_run(SimpleNamespace(name=project_name))
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
        cmd_restart(SimpleNamespace(name=project_name, force=True))
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


def _run_action_interactive(stdscr, action_key: str, project_name: str) -> str:
    action_label = {"run": "run", "build": "build", "stop": "stop", "adapt": "adapt", "remove": "remove", "restart": "restart"}[action_key]
    curses.def_prog_mode()
    curses.endwin()
    print(f"[dashboard] {action_label} {project_name}")
    success, detail = _execute_action(action_key, project_name)
    if detail:
        print(detail)
    try:
        input("Press Enter to return to dashboard...")
    except EOFError:
        pass
    curses.reset_prog_mode()
    stdscr.refresh()
    return f"{action_label} {project_name}: {'ok' if success else 'failed'}"


def _draw_dashboard(stdscr, snapshot: DashboardSnapshot, selected: int, show_help: bool, message: str):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    line = 0
    title = "skua dashboard (auto-refresh: 2s)"
    stdscr.addstr(line, 0, _clip_line(title, width - 1))
    line += 1
    if message:
        stdscr.addstr(line, 0, _clip_line(message, width - 1), curses.color_pair(DashboardColors.HEADER) | curses.A_BOLD)
        line += 1

    if show_help:
        help_lines = [
            "Keys: Up/Down select | Enter run | b build | s stop | a adapt | d remove | r restart",
            "      h toggle help | q quit",
            "",
            "Actions run for the selected project and then return to the dashboard.",
        ]
        for text in help_lines:
            if line >= height:
                break
            stdscr.addstr(line, 0, _clip_line(text, width - 1))
            line += 1
        stdscr.refresh()
        return

    if not snapshot.rows:
        for text in snapshot.summary:
            if line >= height:
                break
            stdscr.addstr(line, 0, _clip_line(text, width - 1))
            line += 1
        if line < height:
            stdscr.addstr(line, 0, _clip_line("Press q to quit. Press h for help.", width - 1))
        stdscr.refresh()
        return

    header = " ".join(f"{title:<{col_width}}" for title, col_width in snapshot.columns)
    stdscr.addstr(line, 0, _clip_line(header, width - 1), curses.color_pair(DashboardColors.HEADER) | curses.A_BOLD)
    line += 1
    divider_len = min(sum(col_width for _, col_width in snapshot.columns) + (len(snapshot.columns) - 1), width - 1)
    stdscr.addstr(line, 0, "-" * max(1, divider_len), curses.color_pair(DashboardColors.HEADER))
    line += 1

    available = max(0, height - line - len(snapshot.summary) - 2)
    start = 0
    if selected >= available > 0:
        start = selected - available + 1
    visible = snapshot.rows[start: start + available] if available else []
    for idx, row in enumerate(visible, start=start):
        if line >= height:
            break
        _draw_row(stdscr, line, width, snapshot.columns, row["cells"], selected=(idx == selected))
        line += 1

    if line < height:
        stdscr.addstr(line, 0, _clip_line("", width - 1))
        line += 1

    for text in snapshot.summary:
        if line >= height:
            break
        stdscr.addstr(line, 0, _clip_line(text, width - 1), _summary_attr(text))
        line += 1
    if line < height:
        stdscr.addstr(line, 0, _clip_line("Press h for help. Press q to quit.", width - 1))
    stdscr.refresh()


def _init_dashboard_colors():
    if not curses.has_colors():
        return
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass

    curses.init_pair(DashboardColors.HEADER, curses.COLOR_CYAN, -1)
    curses.init_pair(DashboardColors.NAME, curses.COLOR_WHITE, -1)
    curses.init_pair(DashboardColors.STATUS_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(DashboardColors.STATUS_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(DashboardColors.STATUS_BAD, curses.COLOR_RED, -1)
    curses.init_pair(DashboardColors.GIT_GOOD, curses.COLOR_GREEN, -1)
    curses.init_pair(DashboardColors.GIT_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(DashboardColors.GIT_BAD, curses.COLOR_RED, -1)
    curses.init_pair(DashboardColors.ACTIVITY_BUSY, curses.COLOR_YELLOW, -1)
    curses.init_pair(DashboardColors.ACTIVITY_IDLE, curses.COLOR_BLUE, -1)
    curses.init_pair(DashboardColors.ACTIVITY_DONE, curses.COLOR_GREEN, -1)
    curses.init_pair(DashboardColors.ACTIVITY_BAD, curses.COLOR_RED, -1)
    curses.init_pair(DashboardColors.SOURCE, curses.COLOR_MAGENTA, -1)
    curses.init_pair(DashboardColors.HOST, curses.COLOR_BLUE, -1)
    curses.init_pair(DashboardColors.IMAGE_WARN, curses.COLOR_MAGENTA, -1)
    curses.init_pair(DashboardColors.AGENT, curses.COLOR_CYAN, -1)


def _summary_attr(text: str) -> int:
    lowered = text.lower()
    if "pending" in lowered or "(a)" in lowered or "(b)" in lowered or "restart is needed" in lowered:
        return curses.color_pair(DashboardColors.STATUS_WARN) | curses.A_BOLD
    if "missing" in lowered or "failed" in lowered:
        return curses.color_pair(DashboardColors.STATUS_BAD) | curses.A_BOLD
    return curses.A_NORMAL


def _column_attr(column: str, value: str) -> int:
    value = str(value or "")
    if column == "NAME":
        return curses.color_pair(DashboardColors.NAME) | curses.A_BOLD
    if column == "HOST":
        if value.startswith("SSH:"):
            return curses.color_pair(DashboardColors.HOST) | curses.A_BOLD
        return curses.color_pair(DashboardColors.HOST)
    if column == "SOURCE":
        if value.startswith("GITHUB:"):
            return curses.color_pair(DashboardColors.STATUS_GOOD)
        if value.startswith("DIR:"):
            return curses.color_pair(DashboardColors.SOURCE)
        return curses.color_pair(DashboardColors.STATUS_WARN)
    if column == "GIT":
        if value in ("CURRENT",):
            return curses.color_pair(DashboardColors.GIT_GOOD)
        if value in ("AHEAD",):
            return curses.color_pair(DashboardColors.HOST) | curses.A_BOLD
        if value in ("BEHIND", "DIVERGED"):
            return curses.color_pair(DashboardColors.GIT_WARN) | curses.A_BOLD
        if value in ("UNCLEAN",):
            return curses.color_pair(DashboardColors.GIT_BAD) | curses.A_BOLD
        return curses.A_DIM
    if column == "ACTIVITY":
        if value in ("-", ""):
            return curses.A_DIM
        if value in ("done",):
            return curses.color_pair(DashboardColors.ACTIVITY_DONE) | curses.A_BOLD
        if value in ("idle",):
            return curses.color_pair(DashboardColors.ACTIVITY_IDLE)
        if value in ("processing", "thinking") or value.startswith("think:"):
            return curses.color_pair(DashboardColors.ACTIVITY_BUSY) | curses.A_BOLD
        if set(value) <= {"X"}:
            if len(value) >= 4:
                return curses.color_pair(DashboardColors.STATUS_WARN) | curses.A_BOLD
            if len(value) >= 2:
                return curses.color_pair(DashboardColors.STATUS_GOOD) | curses.A_BOLD
            return curses.color_pair(DashboardColors.HOST) | curses.A_BOLD
        if value in ("?",):
            return curses.color_pair(DashboardColors.ACTIVITY_BAD) | curses.A_BOLD
        return curses.color_pair(DashboardColors.ACTIVITY_IDLE)
    if column == "STATUS":
        if "!" in value or value.startswith("stale") or value.endswith("*"):
            return curses.color_pair(DashboardColors.STATUS_WARN) | curses.A_BOLD
        if value.startswith("running"):
            return curses.color_pair(DashboardColors.STATUS_GOOD) | curses.A_BOLD
        if value.startswith("built"):
            return curses.color_pair(DashboardColors.HOST)
        if value.startswith("missing") or value.startswith("unreachable"):
            return curses.color_pair(DashboardColors.STATUS_BAD) | curses.A_BOLD
        return curses.A_NORMAL
    if column in ("IMAGE", "RUNNING-IMAGE"):
        if "(A)" in value or "(B)" in value:
            return curses.color_pair(DashboardColors.IMAGE_WARN) | curses.A_BOLD
        if value == "-":
            return curses.A_DIM
        return curses.A_NORMAL
    if column == "AGENT":
        return curses.color_pair(DashboardColors.AGENT) | curses.A_BOLD
    if column == "CREDENTIAL":
        if value == "(none)":
            return curses.A_DIM
        return curses.color_pair(DashboardColors.AGENT)
    if column == "SECURITY":
        if value in ("strict", "proxy"):
            return curses.color_pair(DashboardColors.STATUS_GOOD) | curses.A_BOLD
        return curses.color_pair(DashboardColors.STATUS_WARN)
    if column == "NETWORK":
        if value in ("none",):
            return curses.color_pair(DashboardColors.STATUS_WARN) | curses.A_BOLD
        return curses.color_pair(DashboardColors.HOST)
    return curses.A_NORMAL


def _draw_row(stdscr, y: int, width: int, columns: list, cells: list, selected: bool):
    x = 0
    max_x = max(0, width - 1)
    for col_index, ((title, col_width), value) in enumerate(zip(columns, cells)):
        if x >= max_x:
            break
        text = f"{str(value):<{col_width}}"
        text = _clip_line(text, max_x - x)
        if not text:
            break
        attr = _column_attr(title, str(value))
        if selected:
            attr |= curses.A_REVERSE
        stdscr.addstr(y, x, text, attr)
        x += len(text)
        if col_index < len(columns) - 1 and x < max_x:
            spacer_attr = curses.A_REVERSE if selected else curses.A_NORMAL
            stdscr.addstr(y, x, " ", spacer_attr)
            x += 1


def _dashboard_main(stdscr, args):
    _init_dashboard_colors()
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(2000)

    selected = 0
    show_help = False
    message = ""

    while True:
        snapshot = _collect_snapshot(args)
        if selected >= len(snapshot.rows):
            selected = max(0, len(snapshot.rows) - 1)
        _draw_dashboard(stdscr, snapshot, selected, show_help, message)
        message = ""
        key = stdscr.getch()
        if key == -1:
            continue
        if key in (ord("q"), 27):
            return
        if key in (ord("h"),):
            show_help = not show_help
            continue
        if show_help:
            continue
        if key in (curses.KEY_UP, ord("k")):
            if snapshot.rows:
                selected = max(0, selected - 1)
            continue
        if key in (curses.KEY_DOWN, ord("j")):
            if snapshot.rows:
                selected = min(len(snapshot.rows) - 1, selected + 1)
            continue
        if not snapshot.rows:
            continue

        project_name = snapshot.rows[selected]["name"]
        if key in (10, 13, curses.KEY_ENTER):
            message = _run_action_interactive(stdscr, "run", project_name)
            continue
        if key == ord("b"):
            message = _run_action_interactive(stdscr, "build", project_name)
            continue
        if key == ord("s"):
            message = _run_action_interactive(stdscr, "stop", project_name)
            continue
        if key == ord("a"):
            message = _run_action_interactive(stdscr, "adapt", project_name)
            continue
        if key == ord("d"):
            message = _run_action_interactive(stdscr, "remove", project_name)
            continue
        if key == ord("r"):
            message = _run_action_interactive(stdscr, "restart", project_name)


def cmd_dashboard(args):
    curses.wrapper(_dashboard_main, args)
