# SPDX-License-Identifier: BUSL-1.1
"""skua restart — restart a project container."""

from types import SimpleNamespace

from skua.commands.run import cmd_run
from skua.commands.stop import cmd_stop
from skua.config import ConfigStore
from skua.project_lock import ProjectBusyError, format_project_busy_error, project_operation_lock


def cmd_restart(args):
    name = str(getattr(args, "name", "") or "").strip()
    no_attach = bool(getattr(args, "no_attach", False))
    replace_process = bool(getattr(args, "replace_process", True))
    if not name:
        print("Error: Provide a project name.")
        return

    store = ConfigStore()
    try:
        with project_operation_lock(store, name, "restarting"):
            if not cmd_stop(SimpleNamespace(name=name, force=True), lock_project=False):
                return
            cmd_run(
                SimpleNamespace(name=name, no_attach=no_attach, replace_process=replace_process),
                lock_project=False,
            )
    except ProjectBusyError as exc:
        print(format_project_busy_error(exc, "restart this project"))
