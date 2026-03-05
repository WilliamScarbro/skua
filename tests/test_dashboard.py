# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua dashboard command."""

import argparse
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class TestDashboardSnapshot(unittest.TestCase):
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


class TestDashboardJobs(unittest.TestCase):
    def test_background_command_mapping(self):
        from skua.commands.dashboard import _background_command

        self.assertEqual(
            [sys.executable, "-m", "skua", "build", "demo"],
            _background_command("build", "demo"),
        )
        self.assertEqual(
            [sys.executable, "-m", "skua", "adapt", "demo", "--apply-only", "--force"],
            _background_command("adapt", "demo"),
        )
        self.assertIsNone(_background_command("run", "demo"))

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

        mock_select.side_effect = [
            "Git repository",      # source
            "Remote SSH host",     # run location
            "qar",                 # host
            "local-docker",        # env
            "open",                # security
            "claude",              # agent
        ]
        mock_input.side_effect = ["demo", "git@github.com:org/repo.git", "", ""]

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
        ]
        mock_input.side_effect = [
            "demo",      # name
            ":b",        # back from repo prompt to source selector
            "/tmp",      # directory
            "",          # ssh key
            "",          # env
            "",          # security
            "",          # agent
            "",          # base image
        ]
        with mock.patch("skua.commands.dashboard.find_ssh_keys", return_value=[]):
            args = _prompt_new_project_args()

        self.assertEqual("demo", args.name)
        self.assertEqual("/tmp", args.dir)
        self.assertEqual("", args.repo)
