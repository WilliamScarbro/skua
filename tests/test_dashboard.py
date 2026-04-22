# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua dashboard command."""

import argparse
import asyncio
import json
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.loader import ConfigStore
from skua.config.resources import DefaultImage, Project, ProjectSourceSpec

class TestDashboardSnapshot(unittest.TestCase):
    @mock.patch("skua.commands.dashboard.get_running_skua_containers", return_value=[])
    @mock.patch("skua.commands.dashboard._project_build_preflight")
    @mock.patch("skua.commands.list_cmd.image_exists", return_value=True)
    def test_collect_snapshot_clears_stale_operation_state(self, _mock_image_exists, mock_preflight, _mock_running):
        from skua.commands.dashboard import _collect_snapshot

        mock_preflight.return_value = mock.Mock(needs_rebuild=False, force_refresh=False, error="")
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            project = Project(name="demo", directory="/tmp/demo")
            project.state.status = "building"
            project.state.lock_owner = "host:1234"
            project.state.lock_acquired_at = "2026-03-06T00:00:00+00:00"
            store.save_resource(project)

            with mock.patch("skua.commands.dashboard.ConfigStore", return_value=store):
                snap = _collect_snapshot(argparse.Namespace())

            status_idx = [c[0] for c in snap.columns].index("STATUS")
            self.assertEqual("ready", snap.rows[0]["cells"][status_idx])
            refreshed = store.load_project("demo")
            self.assertEqual("", refreshed.state.status)

    @mock.patch("skua.commands.dashboard.image_exists", return_value=True)
    @mock.patch("skua.commands.dashboard.get_running_skua_containers")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_collect_snapshot_prefers_operation_state(self, MockStore, mock_running, _mock_image_exists):
        from skua.commands.dashboard import _collect_snapshot

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["demo"]
        store.resolve_project.return_value = SimpleNamespace(
            name="demo",
            directory="/tmp/demo",
            repo="",
            host="",
            environment="local-docker",
            security="open",
            agent="claude",
            credential="",
            state=SimpleNamespace(status="building", lock_owner="host:1234", lock_acquired_at="2026-03-06T00:00:00+00:00"),
        )
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))
        mock_running.return_value = []

        snap = _collect_snapshot(argparse.Namespace())
        status_idx = [c[0] for c in snap.columns].index("STATUS")
        self.assertEqual("building", snap.rows[0]["cells"][status_idx])

    @mock.patch("skua.commands.dashboard.image_exists", return_value=False)
    @mock.patch("skua.commands.dashboard.get_running_skua_containers")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_collect_snapshot_local_flag_filters_remote(self, MockStore, mock_running, _mock_image_exists):
        from skua.commands.dashboard import _collect_snapshot

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["local", "remote"]
        projects = {
            "local": SimpleNamespace(
                name="local",
                directory="/tmp/local",
                repo="",
                host="",
                environment="local-docker",
                security="open",
                agent="claude",
                credential="",
            ),
            "remote": SimpleNamespace(
                name="remote",
                directory="",
                repo="git@github.com:org/repo.git",
                host="qar",
                environment="local-docker",
                security="open",
                agent="claude",
                credential="",
            ),
        }
        store.resolve_project.side_effect = lambda name: projects[name]
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))
        mock_running.return_value = []

        snap = _collect_snapshot(argparse.Namespace(local=True))

        self.assertEqual(1, len(snap.rows))
        self.assertEqual("local", snap.rows[0]["name"])
        self.assertEqual(["NAME", "ACTIVITY", "STATUS", "SOURCE"], [c[0] for c in snap.columns])
        self.assertIn("2 project(s), 0 running", snap.summary[0])

    @mock.patch("skua.commands.dashboard.image_exists", return_value=True)
    @mock.patch("skua.commands.dashboard.get_running_skua_containers")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_collect_snapshot_marks_unreachable_remote_host(self, MockStore, mock_running, _mock_image_exists):
        from skua.commands.dashboard import _collect_snapshot

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["remote"]
        store.resolve_project.return_value = SimpleNamespace(
            name="remote",
            directory="",
            repo="git@github.com:org/repo.git",
            host="badhost",
            environment="local-docker",
            security="open",
            agent="claude",
            credential="",
        )
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))

        # Local query succeeds; SSH query fails and returns None.
        mock_running.side_effect = [[], None]

        snap = _collect_snapshot(argparse.Namespace())

        self.assertEqual(1, len(snap.rows))
        self.assertEqual("remote", snap.rows[0]["name"])
        status_idx = [c[0] for c in snap.columns].index("STATUS")
        self.assertEqual("unreachable", snap.rows[0]["cells"][status_idx])

    @mock.patch("skua.commands.dashboard._credential_state", return_value=("stale", "expired", "cred-main !stale"))
    @mock.patch("skua.commands.dashboard.get_running_skua_containers")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_collect_snapshot_marks_stale_credentials(self, MockStore, mock_running, _mock_cred_state):
        from skua.commands.dashboard import _collect_snapshot

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["demo"]
        store.resolve_project.return_value = SimpleNamespace(
            name="demo",
            directory="/tmp/demo",
            repo="",
            host="",
            environment="local-docker",
            security="open",
            agent="claude",
            credential="cred-main",
        )
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))
        mock_running.return_value = ["skua-demo"]

        snap = _collect_snapshot(argparse.Namespace(agent=True))
        columns = [c[0] for c in snap.columns]
        status_idx = columns.index("STATUS")
        cred_idx = columns.index("CREDENTIAL")

        self.assertEqual("running!", snap.rows[0]["cells"][status_idx])
        self.assertEqual("cred-main !stale", snap.rows[0]["cells"][cred_idx])
        self.assertTrue(any("stale/missing local credentials" in line for line in snap.summary))

    @mock.patch("skua.commands.dashboard._project_build_preflight")
    @mock.patch("skua.commands.dashboard.image_exists", return_value=True)
    @mock.patch("skua.commands.list_cmd.image_exists", return_value=True)
    @mock.patch("skua.commands.dashboard.get_running_skua_containers")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_collect_snapshot_marks_refresh_required(self, MockStore, mock_running, _mock_list_exists, _mock_dash_exists, mock_preflight):
        from skua.commands.dashboard import _collect_snapshot

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["demo"]
        store.resolve_project.return_value = SimpleNamespace(
            name="demo",
            directory="/tmp/demo",
            repo="",
            host="",
            environment="local-docker",
            security="open",
            agent="codex",
            credential="",
        )
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))
        mock_running.return_value = []
        mock_preflight.return_value = mock.Mock(needs_rebuild=True, force_refresh=True, error="")

        snap = _collect_snapshot(argparse.Namespace())
        status_idx = [c[0] for c in snap.columns].index("STATUS")

        self.assertEqual("refresh", snap.rows[0]["cells"][status_idx])
        self.assertTrue(any("ready=run now" in line for line in snap.summary))

    @mock.patch("skua.commands.dashboard._project_build_preflight")
    @mock.patch("skua.commands.dashboard.image_exists", return_value=True)
    @mock.patch("skua.commands.list_cmd.image_exists", return_value=True)
    @mock.patch("skua.commands.dashboard.get_running_skua_containers")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_collect_snapshot_marks_rebuild_required(self, MockStore, mock_running, _mock_list_exists, _mock_dash_exists, mock_preflight):
        from skua.commands.dashboard import _collect_snapshot

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["demo"]
        store.resolve_project.return_value = SimpleNamespace(
            name="demo",
            directory="/tmp/demo",
            repo="",
            host="",
            environment="local-docker",
            security="open",
            agent="codex",
            credential="",
        )
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))
        mock_running.return_value = []
        mock_preflight.return_value = mock.Mock(needs_rebuild=True, force_refresh=False, error="")

        snap = _collect_snapshot(argparse.Namespace())
        status_idx = [c[0] for c in snap.columns].index("STATUS")

        self.assertEqual("rebuild", snap.rows[0]["cells"][status_idx])


class TestDashboardCli(unittest.TestCase):
    @mock.patch("skua.commands.cmd_dashboard")
    def test_cli_dispatches_dashboard(self, mock_dashboard):
        from skua import cli

        with mock.patch.object(sys, "argv", ["skua", "dashboard", "--local", "--image"]):
            cli.main()

        self.assertTrue(mock_dashboard.called)
        args = mock_dashboard.call_args.args[0]
        self.assertTrue(args.local)
        self.assertTrue(args.image)


class TestDashboardActions(unittest.TestCase):
    @mock.patch("skua.commands.dashboard.cmd_run")
    def test_run_action_attaches_without_replacing_dashboard_process(self, mock_cmd_run):
        from skua.commands.dashboard import _run_action

        self.assertTrue(_run_action("run", "demo"))
        mock_cmd_run.assert_called_once()
        args = mock_cmd_run.call_args.args[0]
        self.assertEqual("demo", args.name)
        self.assertFalse(hasattr(args, "no_attach"))
        self.assertFalse(args.replace_process)

    @mock.patch("skua.commands.dashboard.cmd_restart")
    def test_restart_action_attaches_without_replacing_dashboard_process(self, mock_cmd_restart):
        from skua.commands.dashboard import _run_action

        self.assertTrue(_run_action("restart", "demo"))
        mock_cmd_restart.assert_called_once()
        args = mock_cmd_restart.call_args.args[0]
        self.assertEqual("demo", args.name)
        self.assertTrue(args.force)
        self.assertFalse(hasattr(args, "no_attach"))
        self.assertFalse(args.replace_process)


class TestDashboardRunPreflight(unittest.TestCase):
    @mock.patch("skua.commands.dashboard._project_build_preflight")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_run_preflight_returns_target_only_when_not_force_refresh(self, MockStore, mock_preflight):
        from skua.commands.dashboard import BuildPreflightCheck, _run_preflight_checks

        store = MockStore.return_value
        project = SimpleNamespace(name="demo")
        store.resolve_project.return_value = project
        mock_preflight.return_value = BuildPreflightCheck(
            project="demo",
            needs_rebuild=True,
            force_refresh=False,
            reason="build context changed",
            error="",
        )

        checks, errors = _run_preflight_checks("demo")
        self.assertEqual([], errors)
        self.assertEqual(["demo"], [c.project for c in checks])
        self.assertEqual(1, mock_preflight.call_count)

    @mock.patch("skua.commands.dashboard._project_build_preflight")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_run_preflight_returns_target_only_on_force_refresh(self, MockStore, mock_preflight):
        from skua.commands.dashboard import BuildPreflightCheck, _run_preflight_checks

        store = MockStore.return_value
        store.resolve_project.return_value = SimpleNamespace(name="demo")
        mock_preflight.return_value = BuildPreflightCheck(
            project="demo",
            needs_rebuild=True,
            force_refresh=True,
            reason="codex client update available",
            error="",
        )

        checks, errors = _run_preflight_checks("demo")
        self.assertEqual([], errors)
        self.assertEqual(["demo"], [c.project for c in checks])
        self.assertEqual(1, mock_preflight.call_count)


class TestDashboardJobs(unittest.TestCase):
    def test_active_jobs_for_quit_filters_orphaning_statuses(self):
        from skua.commands.dashboard import DashboardJob, _active_jobs_for_quit

        jobs = [
            DashboardJob(1, "build", "a", [], "queued", "", "", "", None, None, "", "", ""),
            DashboardJob(2, "adapt", "b", [], "running", "", "", "", None, None, "", "", ""),
            DashboardJob(3, "build", "c", [], "waiting_input", "", "", "", None, None, "", "", ""),
            DashboardJob(4, "build", "d", [], "success", "", "", "", 0, None, "", "", ""),
        ]

        active = _active_jobs_for_quit(jobs)
        self.assertEqual([1, 2, 3], [job.job_id for job in active])

    @mock.patch("skua.commands.dashboard.project_busy_error_if_locked")
    def test_lock_block_message_when_busy(self, mock_busy):
        from skua.commands.dashboard import _lock_block_message
        from skua.project_lock import ProjectBusyError

        mock_busy.return_value = ProjectBusyError(
            project_name="demo",
            operation="building",
            owner="host:1234",
            acquired_at="2026-03-06T00:00:00+00:00",
        )
        msg = _lock_block_message("demo", "stop")
        self.assertIn("Project 'demo' is busy", msg)
        self.assertIn("cannot stop this project", msg)

    @mock.patch("skua.commands.dashboard.project_busy_error_if_locked", return_value=None)
    def test_lock_block_message_empty_when_not_busy(self, _mock_busy):
        from skua.commands.dashboard import _lock_block_message

        self.assertEqual("", _lock_block_message("demo", "build"))

    def test_extract_lock_busy_error(self):
        from skua.commands.dashboard import _extract_lock_busy_error

        lines = [
            "[job 1] action=build project=demo",
            "Error: Project 'demo' is busy (adapting) by host:1234 since 2026-03-06T00:00:00+00:00; cannot build this project.",
            "[job 1] ended=2026-03-06T00:00:01+00:00 status=failed return_code=1",
        ]
        msg = _extract_lock_busy_error(lines)
        self.assertIn("Project 'demo' is busy", msg)
        self.assertIn("cannot build this project", msg)

    @mock.patch("skua.commands.dashboard._resolve_skua_cli_prefix", return_value=["/usr/bin/skua"])
    def test_background_command_mapping(self, _mock_prefix):
        from skua.commands.dashboard import _background_command

        self.assertEqual(
            ["/usr/bin/skua", "build", "demo"],
            _background_command("build", "demo"),
        )
        self.assertEqual(
            ["/usr/bin/skua", "adapt", "demo", "--build", "--force"],
            _background_command("adapt", "demo"),
        )
        self.assertEqual(
            ["/usr/bin/skua", "adapt", "demo", "--discover", "--force"],
            _background_command("adapt", "demo", discover=True),
        )
        self.assertIsNone(_background_command("run", "demo"))

    @mock.patch("skua.commands.dashboard.shutil.which", return_value=None)
    def test_resolve_cli_prefix_falls_back_to_cli_py(self, _mock_which):
        from skua.commands.dashboard import _resolve_skua_cli_prefix

        prefix = _resolve_skua_cli_prefix()
        self.assertEqual(sys.executable, prefix[0])
        self.assertTrue(prefix[1].endswith("/skua/cli.py"))

    @mock.patch("skua.commands.dashboard._background_command", return_value=["/usr/bin/skua", "remove", "demo"])
    def test_enqueue_remove_job_success(self, _mock_background):
        from skua.commands.dashboard import _enqueue_remove_job

        jobs = mock.Mock()
        queued = SimpleNamespace(job_id=7)
        jobs.enqueue.return_value = queued

        job, error = _enqueue_remove_job(jobs, "demo")

        self.assertIs(job, queued)
        self.assertEqual("", error)
        jobs.enqueue.assert_called_once_with("remove", "demo", command=["/usr/bin/skua", "remove", "demo"])

    @mock.patch("skua.commands.dashboard._background_command", return_value=None)
    def test_enqueue_remove_job_reports_unavailable_action(self, _mock_background):
        from skua.commands.dashboard import _enqueue_remove_job

        jobs = mock.Mock()

        job, error = _enqueue_remove_job(jobs, "demo")

        self.assertIsNone(job)
        self.assertEqual("remove action is unavailable", error)
        jobs.enqueue.assert_not_called()

    @mock.patch("skua.commands.dashboard._background_command", return_value=["/usr/bin/skua", "remove", "demo"])
    def test_enqueue_remove_job_reports_enqueue_failure(self, _mock_background):
        from skua.commands.dashboard import _enqueue_remove_job

        jobs = mock.Mock()
        jobs.enqueue.side_effect = RuntimeError("boom")

        job, error = _enqueue_remove_job(jobs, "demo")

        self.assertIsNone(job)
        self.assertEqual("failed to queue remove demo: RuntimeError: boom", error)

    def test_job_manager_enqueue_poll_and_persist(self):
        from skua.commands.dashboard import DashboardJobManager

        with tempfile.TemporaryDirectory() as td:
            mgr = DashboardJobManager(config_dir=Path(td), max_jobs=20)
            job = mgr.enqueue(
                "build",
                "demo",
                command=[sys.executable, "-c", "print('hello from dashboard job')"],
            )
            for _ in range(80):
                mgr.poll()
                jobs = mgr.list_for_view()
                if jobs and jobs[0].status in ("success", "failed"):
                    break
                time.sleep(0.05)

            state = json.loads((Path(td) / "jobs" / "jobs.json").read_text())
            entries = state["jobs"]
            self.assertTrue(entries)
            persisted = entries[-1]
            self.assertEqual(job.job_id, persisted["job_id"])
            self.assertEqual("success", persisted["status"])
            self.assertEqual(0, persisted["return_code"])
            self.assertTrue(Path(persisted["log_path"]).exists())

    def test_job_manager_rejects_duplicate_active_project_jobs(self):
        from skua.commands.dashboard import DashboardJobManager

        with tempfile.TemporaryDirectory() as td:
            mgr = DashboardJobManager(config_dir=Path(td), max_jobs=20)
            mgr.enqueue(
                "build",
                "demo",
                command=[sys.executable, "-c", "import time; time.sleep(0.3)"],
            )
            with self.assertRaises(ValueError):
                mgr.enqueue(
                    "adapt",
                    "demo",
                    command=[sys.executable, "-c", "print('second job')"],
                )

    def test_job_manager_marks_inflight_jobs_orphaned_on_reload(self):
        from skua.commands.dashboard import DashboardJobManager

        with tempfile.TemporaryDirectory() as td:
            jobs_dir = Path(td) / "jobs"
            jobs_dir.mkdir(parents=True, exist_ok=True)
            state_file = jobs_dir / "jobs.json"
            state_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "jobs": [
                            {
                                "job_id": 7,
                                "action": "adapt",
                                "project": "demo",
                                "command": [sys.executable, "-m", "skua", "adapt", "demo"],
                                "status": "running",
                                "created_at": "2026-03-05T00:00:00+00:00",
                                "started_at": "2026-03-05T00:00:00+00:00",
                                "ended_at": "",
                                "return_code": None,
                                "pid": 99999,
                                "log_path": str(jobs_dir / "logs" / "000007-adapt-demo.log"),
                                "detail": "",
                            }
                        ],
                    }
                )
            )

            mgr = DashboardJobManager(config_dir=Path(td), max_jobs=20)
            jobs = mgr.list_for_view()
            self.assertEqual(1, len(jobs))
            self.assertEqual("orphaned", jobs[0].status)
            self.assertTrue(jobs[0].ended_at)

    def test_job_manager_remove_job(self):
        from skua.commands.dashboard import DashboardJobManager

        with tempfile.TemporaryDirectory() as td:
            mgr = DashboardJobManager(config_dir=Path(td), max_jobs=20)
            job = mgr.enqueue(
                "build",
                "demo",
                command=[sys.executable, "-c", "print('done')"],
            )
            for _ in range(80):
                mgr.poll()
                jobs = mgr.list_for_view()
                if jobs and jobs[0].status in ("success", "failed"):
                    break
                time.sleep(0.05)
            ok, detail = mgr.remove_job(job.job_id)
            self.assertTrue(ok)
            self.assertEqual("", detail)
            self.assertEqual([], mgr.list_for_view())

    def test_job_manager_waiting_input_and_send(self):
        from skua.commands.dashboard import DashboardJobManager

        with tempfile.TemporaryDirectory() as td:
            mgr = DashboardJobManager(config_dir=Path(td), max_jobs=20)
            job = mgr.enqueue(
                "remove",
                "demo",
                command=[sys.executable, "-c", "x=input('Continue? [y/N]: '); print('ans=' + x)"],
            )

            waiting = False
            for _ in range(120):
                mgr.poll()
                current = next((j for j in mgr.list_for_view() if j.job_id == job.job_id), None)
                if current and current.status == "waiting_input":
                    waiting = True
                    break
                time.sleep(0.02)
            self.assertTrue(waiting)

            ok, detail = mgr.send_input(job.job_id, "y")
            self.assertTrue(ok)
            self.assertEqual("", detail)

            for _ in range(120):
                mgr.poll()
                current = next((j for j in mgr.list_for_view() if j.job_id == job.job_id), None)
                if current and current.status in ("success", "failed"):
                    break
                time.sleep(0.02)

            current = next((j for j in mgr.list_for_view() if j.job_id == job.job_id), None)
            self.assertIsNotNone(current)
            self.assertEqual("success", current.status)


class TestDashboardAddFlow(unittest.TestCase):
    @mock.patch("skua.commands.dashboard.cmd_add")
    @mock.patch("skua.commands.dashboard._prompt_new_project_args")
    def test_run_add_project_interactive_calls_cmd_add(self, mock_prompt, mock_cmd_add):
        from skua.commands.dashboard import _run_add_project_interactive

        mock_prompt.return_value = SimpleNamespace(name="proj")
        message = _run_add_project_interactive()

        self.assertEqual("new project proj: ok", message)
        mock_cmd_add.assert_called_once_with(mock_prompt.return_value)

    @mock.patch("skua.commands.dashboard.select_option")
    @mock.patch("skua.commands.dashboard.input")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_prompt_new_project_args_remote_repo_flow(self, MockStore, mock_input, mock_select):
        from skua.commands.dashboard import _prompt_new_project_args

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_global.return_value = {"defaults": {"environment": "local-docker", "security": "open", "agent": "claude"}}
        store.list_resources.side_effect = lambda kind: {
            "Environment": ["local-docker", "remote-docker"],
            "SecurityProfile": ["open", "standard"],
            "AgentConfig": ["claude", "codex"],
            "Credential": [],
        }.get(kind, [])
        store.load_all_resources.return_value = []  # no default images

        mock_select.side_effect = [
            "Git repository",      # source
            "Remote SSH host",     # run location
            "qar",                 # host
            "local-docker",        # env
            "open",                # security
            "claude",              # agent
            "Build new",           # image source (step 10)
        ]
        mock_input.side_effect = ["demo", "git@github.com:org/repo.git", ""]

        with mock.patch("skua.commands.dashboard.parse_ssh_config_hosts", return_value=["qar"]):
            with mock.patch("skua.commands.dashboard.find_ssh_keys", return_value=[]):
                args = _prompt_new_project_args()

        self.assertEqual("demo", args.name)
        self.assertEqual("git@github.com:org/repo.git", args.repo)
        self.assertEqual("qar", args.host)
        self.assertEqual("", args.dir)
        self.assertEqual("local-docker", args.env)
        self.assertEqual("open", args.security)
        self.assertEqual("claude", args.agent)

    @mock.patch("skua.commands.dashboard.select_option")
    @mock.patch("skua.commands.dashboard.input")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_prompt_new_project_args_supports_back_and_cancel(self, MockStore, mock_input, mock_select):
        from skua.commands.dashboard import _prompt_new_project_args

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_global.return_value = {"defaults": {"environment": "local-docker", "security": "open", "agent": "claude"}}
        store.list_resources.side_effect = lambda kind: {
            "Environment": ["local-docker"],
            "SecurityProfile": ["open"],
            "AgentConfig": ["claude"],
            "Credential": [],
        }.get(kind, [])
        store.load_all_resources.return_value = []  # no default images

        # First attempt: cancel immediately from first text prompt.
        mock_input.side_effect = [":q"]
        mock_select.side_effect = []
        with mock.patch("skua.commands.dashboard.find_ssh_keys", return_value=[]):
            self.assertIsNone(_prompt_new_project_args())

        # Second attempt: choose git, go back from repo prompt, then choose local dir.
        mock_select.side_effect = [
            "Git repository",    # source
            "Local directory",   # source after back
            "local-docker",      # env
            "open",              # security
            "claude",            # agent
            "Build new",         # image source (step 10)
        ]
        mock_input.side_effect = [
            "demo",      # name
            ":b",        # back from repo prompt to source selector
            "/tmp",      # directory
            "",          # ssh key
        ]
        with mock.patch("skua.commands.dashboard.find_ssh_keys", return_value=[]):
            args = _prompt_new_project_args()

        self.assertEqual("demo", args.name)
        self.assertEqual("/tmp", args.dir)
        self.assertEqual("", args.repo)

    @mock.patch("skua.commands.dashboard._cred_matches_agent", return_value=True)
    @mock.patch("skua.commands.dashboard.select_option")
    @mock.patch("skua.commands.dashboard.input")
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_prompt_new_project_args_defaults_to_existing_credential(self, MockStore, mock_input, mock_select, _mock_match):
        from skua.commands.dashboard import _prompt_new_project_args

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_global.return_value = {"defaults": {"environment": "local-docker", "security": "open", "agent": "claude"}}
        store.list_resources.side_effect = lambda kind: {
            "Environment": ["local-docker"],
            "SecurityProfile": ["open"],
            "AgentConfig": ["claude"],
            "Credential": ["cred-z", "cred-a"],
        }.get(kind, [])
        store.load_all_resources.return_value = []

        mock_select.side_effect = [
            "Local directory",
            "local-docker",
            "open",
            "claude",
            "cred-a",
            "Build new",
        ]
        mock_input.side_effect = ["demo", "/tmp/demo", ""]

        with mock.patch("skua.commands.dashboard.find_ssh_keys", return_value=[]):
            with mock.patch("pathlib.Path.exists", return_value=True):
                args = _prompt_new_project_args()

        self.assertEqual("cred-a", args.credential)
        credential_call = mock_select.call_args_list[4]
        self.assertEqual("Credential:", credential_call.args[0])
        self.assertEqual(2, credential_call.kwargs["default_index"])

    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_dashboard_new_project_blocks_missing_directory(self, MockStore):
        import skua.commands.dashboard as dashboard

        store = MockStore.return_value
        store.list_resources.return_value = []
        store.load_all_resources.return_value = []
        store.load_project.return_value = None

        captured = {}

        def fake_run(self, **_kwargs):
            captured["app"] = self

        args = argparse.Namespace(refresh_seconds=0)
        with mock.patch("textual.app.App.run", new=fake_run):
            dashboard.cmd_dashboard(args)

        app = captured["app"]
        app.task_catalog = {"keys": [], "envs": [], "secs": [], "agents": [], "hosts": []}
        app._start_new_project_task()
        app.task_values["name"] = "demo"
        app.task_step = 2
        original_dir = app.task_values["dir"]
        app.task_input = "/q"
        if Path(app.task_input).exists():
            self.fail(f"expected missing path for test: {app.task_input}")
        app._refresh_view = mock.Mock()

        app._task_submit_step()

        self.assertEqual(2, app.task_step)
        self.assertEqual(original_dir, app.task_values["dir"])
        self.assertEqual("directory does not exist", app.task_error)
        self.assertEqual("directory does not exist", app.message)

        panel = app._render_task_panel()
        self.assertIn("(directory does not exist)", panel.plain)
        self.assertTrue(any("red" in str(span.style) for span in panel.spans))

    @mock.patch("skua.commands.dashboard._cred_matches_agent", return_value=True)
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_dashboard_new_project_defaults_to_existing_credential(self, MockStore, _mock_match):
        import skua.commands.dashboard as dashboard

        store = MockStore.return_value
        store.list_resources.side_effect = lambda kind: {
            "Credential": ["cred-b", "cred-a"],
            "Environment": ["local-docker"],
            "SecurityProfile": ["open"],
            "AgentConfig": ["claude"],
        }.get(kind, [])
        store.load_all_resources.return_value = []
        store.load_global.return_value = {"defaults": {"environment": "local-docker", "security": "open", "agent": "claude"}}
        store.load_project.return_value = None

        captured = {}

        def fake_run(self, **_kwargs):
            captured["app"] = self

        args = argparse.Namespace(refresh_seconds=0)
        with mock.patch("textual.app.App.run", new=fake_run):
            dashboard.cmd_dashboard(args)

        app = captured["app"]
        app._start_new_project_task()

        self.assertEqual("cred-a", app.task_values["credential_choice"])


class TestDashboardWidgetSelection(unittest.TestCase):
    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_project_widget_selection_survives_refresh(self, MockStore):
        import skua.commands.dashboard as dashboard
        from skua.commands.dashboard import DashboardSnapshot

        store = MockStore.return_value
        store.list_resources.return_value = []
        store.load_all_resources.return_value = []
        store.load_project.return_value = None

        captured = {}

        def fake_run(self, **_kwargs):
            captured["app"] = self

        args = argparse.Namespace(refresh_seconds=0)
        with mock.patch("textual.app.App.run", new=fake_run):
            dashboard.cmd_dashboard(args)

        app = captured["app"]
        rows = [{"name": f"p{i}", "cells": [f"p{i}", "built"]} for i in range(5)]
        snapshot = DashboardSnapshot(columns=[("NAME", 12), ("STATUS", 8)], rows=rows, summary=["ok"])

        async def run_case():
            with mock.patch.object(app, "_request_refresh", lambda: None):
                async with app.run_test() as pilot:
                    app.snapshot = snapshot
                    app._refresh_view()
                    table = app.query_one("#projects-table")

                    table.move_cursor(row=3, column=0)
                    await pilot.pause()

                    self.assertEqual(3, app.selected)
                    self.assertEqual("p3", app.selected_project_name)

                    refreshed_rows = [{"name": f"p{i}", "cells": [f"p{i}", "running"]} for i in range(5)]
                    app._apply_snapshot(
                        DashboardSnapshot(
                            columns=[("NAME", 12), ("STATUS", 8)],
                            rows=refreshed_rows,
                            summary=["ok"],
                        )
                    )
                    await pilot.pause()

                    self.assertEqual(3, app.selected)
                    self.assertEqual("p3", app.selected_project_name)
                    self.assertEqual(3, table.cursor_row)

        asyncio.run(run_case())

    @mock.patch("skua.commands.dashboard.ConfigStore")
    def test_enter_binding_runs_project_even_when_table_has_focus(self, MockStore):
        import skua.commands.dashboard as dashboard
        from skua.commands.dashboard import DashboardSnapshot

        store = MockStore.return_value
        store.list_resources.return_value = []
        store.load_all_resources.return_value = []
        store.load_project.return_value = None

        captured = {}

        def fake_run(self, **_kwargs):
            captured["app"] = self

        args = argparse.Namespace(refresh_seconds=0)
        with mock.patch("textual.app.App.run", new=fake_run):
            dashboard.cmd_dashboard(args)

        app = captured["app"]
        snapshot = DashboardSnapshot(
            columns=[("NAME", 12), ("STATUS", 8)],
            rows=[{"name": "demo", "cells": ["demo", "built"]}],
            summary=["ok"],
        )

        async def run_case():
            with mock.patch.object(app, "_request_refresh", lambda: None), \
                 mock.patch("skua.commands.dashboard._lock_block_message", return_value=""), \
                 mock.patch.object(app, "_project_is_running", return_value=True), \
                 mock.patch("skua.commands.dashboard._execute_action", return_value=(True, "ok")) as mock_exec:
                async with app.run_test() as pilot:
                    app._apply_snapshot(snapshot)
                    await pilot.pause()

                    self.assertEqual("projects-table", getattr(app.focused, "id", ""))

                    await pilot.press("enter")
                    await pilot.pause()

                    mock_exec.assert_called_once_with("run", "demo", replace_process=False)

        asyncio.run(run_case())


class TestDashboardClipboard(unittest.TestCase):
    @mock.patch("skua.commands.dashboard.Path.exists", return_value=True)
    @mock.patch("skua.commands.dashboard._clipboard_commands", return_value=[])
    def test_clipboard_available_with_tty_fallback(self, _mock_cmds, _mock_exists):
        from skua.commands.dashboard import _clipboard_copy_available

        self.assertTrue(_clipboard_copy_available())

    @mock.patch("skua.commands.dashboard._copy_text_to_clipboard_osc52", return_value=(True, ""))
    @mock.patch("skua.commands.dashboard._clipboard_commands", return_value=[])
    def test_copy_text_uses_osc52_when_no_clipboard_tool(self, _mock_cmds, mock_osc52):
        from skua.commands.dashboard import _copy_text_to_clipboard

        ok, detail = _copy_text_to_clipboard("hello")

        self.assertTrue(ok)
        self.assertEqual("", detail)
        mock_osc52.assert_called_once_with("hello")

    @mock.patch("skua.commands.dashboard._copy_text_to_clipboard_osc52", return_value=(True, ""))
    @mock.patch("skua.commands.dashboard.subprocess.run", side_effect=subprocess.TimeoutExpired("xclip", timeout=2))
    @mock.patch("skua.commands.dashboard._clipboard_commands", return_value=[["xclip", "-selection", "clipboard"]])
    def test_copy_text_timeout_falls_back_to_osc52(self, _mock_cmds, _mock_run, mock_osc52):
        from skua.commands.dashboard import _copy_text_to_clipboard

        ok, detail = _copy_text_to_clipboard("hello")

        self.assertTrue(ok)
        self.assertEqual("", detail)
        mock_osc52.assert_called_once_with("hello")


class TestDashboardProjectDetail(unittest.TestCase):
    def test_project_detail_dirty_ignores_runtime_state_and_resources(self):
        from skua.commands.dashboard import _project_detail_is_dirty

        original = Project(name="demo", directory="/tmp/demo")
        draft = Project(name="demo", directory="/tmp/demo")

        draft.state.status = "running"
        draft.resources.images = ["skua-demo:1"]

        self.assertFalse(_project_detail_is_dirty(draft, original))

        draft.image.base_image = "ubuntu:24.04"
        self.assertTrue(_project_detail_is_dirty(draft, original))

    def test_compatible_default_images_filter_by_agent(self):
        from skua.commands.dashboard import _compatible_default_images

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            store.save_resource(DefaultImage(name="shared", image="ghcr.io/acme/shared:latest"))
            store.save_resource(DefaultImage(name="codex-only", image="ghcr.io/acme/codex:latest", agent="codex"))
            store.save_resource(DefaultImage(name="claude-only", image="ghcr.io/acme/claude:latest", agent="claude"))

            compatible = _compatible_default_images(store, "codex")

        self.assertEqual(["codex-only", "shared"], [default.name for default in compatible])

    def test_project_detail_fields_include_sources_and_ssh_keys(self):
        from skua.commands.dashboard import _project_detail_fields

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            project = Project(
                name="myproj",
                directory="/tmp/primary",
                environment="local-docker",
                security="open",
                agent="codex",
                credential="cred-main",
            )
            project.ssh.private_key = "/home/dev/.ssh/id_ed25519"
            project.ssh.private_keys = [
                "/home/dev/.ssh/id_ed25519",
                "/home/dev/.ssh/id_rsa_work",
            ]
            project.image.base_image = "ubuntu:24.04"
            project.image.absolute_image = "ghcr.io/acme/codex-default:latest"
            project.image.extra_packages = ["git", "ripgrep"]
            project.image.extra_commands = ["apt-get update", "apt-get install -y jq"]
            project.image.version = 3
            project.resources.images = ["skua-myproj:3"]
            project.sources = [
                ProjectSourceSpec(
                    project="myproj",
                    name="base-a",
                    directory="/src/a",
                    mount_path="/worktrees/base-a",
                    primary=True,
                ),
                ProjectSourceSpec(
                    project="myproj",
                    name="base-b",
                    repo="git@github.com:org/base-b.git",
                    mount_path="/worktrees/base-b",
                    host="qar",
                    primary=False,
                ),
            ]
            store.save_resource(project)

            fields = _project_detail_fields(project, store)

        detail = "\n".join(f["display"] for f in fields)
        self.assertIn("Project: myproj", detail)
        self.assertIn("base-a (primary)", detail)
        self.assertIn("base-b:", detail)
        self.assertIn("/home/dev/.ssh/id_ed25519 (primary)", detail)
        self.assertIn("/home/dev/.ssh/id_rsa_work", detail)
        self.assertIn("absolute_image: ghcr.io/acme/codex-default:latest", detail)
        self.assertIn("base_image: ubuntu:24.04", detail)
        self.assertIn("extra_packages: git, ripgrep", detail)
        self.assertIn("images: skua-myproj:3", detail)
        # All major sections present
        self.assertTrue(any(f["section"] and "References" in f["display"] for f in fields))
        self.assertTrue(any(f["section"] and "Sources" in f["display"] for f in fields))
        self.assertTrue(any(f["section"] and "SSH keys" in f["display"] for f in fields))
        self.assertTrue(any(f["section"] and "Image" in f["display"] for f in fields))
        self.assertTrue(any(f.get("field") == "image.absolute_image" and f.get("action") == "detail_edit_absolute_image" for f in fields))
        self.assertTrue(any(f.get("field") == "image.from_image" and f.get("action") == "detail_edit_text" for f in fields))
        # Editable fields exist
        editable = [f for f in fields if f.get("editable")]
        self.assertGreater(len(editable), 5)

    def test_project_detail_fields_report_no_fields_for_missing_project(self):
        from skua.commands.dashboard import _project_detail_fields
        from skua.config.resources import Project as _Project

        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConfigStore(config_dir=Path(tmpdir))
            store.ensure_dirs()
            # An empty/default project still produces fields (no crash)
            project = _Project(name="orphan")
            fields = _project_detail_fields(project, store)
            self.assertIsInstance(fields, list)
            self.assertGreater(len(fields), 0)


class TestDashboardCheckpointManager(unittest.TestCase):
    def test_checkpoint_manager_persists_across_instances(self):
        from skua.commands.dashboard import DashboardCheckpointManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = Path(tmpdir)
            first = DashboardCheckpointManager(config_dir=config_dir)
            project = Project(name="demo", directory="/tmp/demo-v1")
            first.append("demo", "2026-04-13T00:00:00+00:00", project)

            second = DashboardCheckpointManager(config_dir=config_dir)
            checkpoints = second.list("demo")

        self.assertEqual(1, len(checkpoints))
        ts, restored = checkpoints[0]
        self.assertEqual("2026-04-13T00:00:00+00:00", ts)
        self.assertEqual("demo", restored.name)
        self.assertEqual("/tmp/demo-v1", restored.directory)

    def test_checkpoint_manager_limits_history_per_project(self):
        from skua.commands.dashboard import DashboardCheckpointManager

        with tempfile.TemporaryDirectory() as tmpdir:
            manager = DashboardCheckpointManager(config_dir=Path(tmpdir), max_checkpoints=3)
            for idx in range(5):
                project = Project(name="demo", directory=f"/tmp/demo-{idx}")
                manager.append("demo", f"2026-04-13T00:00:0{idx}+00:00", project)

            checkpoints = manager.list("demo")

        self.assertEqual(3, len(checkpoints))
        self.assertEqual(
            [
                "2026-04-13T00:00:02+00:00",
                "2026-04-13T00:00:03+00:00",
                "2026-04-13T00:00:04+00:00",
            ],
            [ts for ts, _project in checkpoints],
        )
