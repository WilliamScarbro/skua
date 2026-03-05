# SPDX-License-Identifier: BUSL-1.1
"""Tests for add/list credential behavior."""

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

# Ensure the skua package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.resources import AgentAuthSpec, AgentConfig, Credential, Project


class TestAddCredentialSelection(unittest.TestCase):
    @staticmethod
    def _args(**kwargs):
        defaults = dict(
            name="test-proj",
            dir=None,
            repo="https://github.com/u/r.git",
            ssh_key="",
            env=None,
            security=None,
            agent=None,
            credential=None,
            no_credential=False,
            quick=True,
            no_prompt=True,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    @staticmethod
    def _claude_agent():
        return AgentConfig(
            name="claude",
            auth=AgentAuthSpec(
                dir=".claude",
                files=[".credentials.json", ".claude.json"],
                login_command="claude /login",
            ),
        )

    @mock.patch("skua.commands.add.ConfigStore")
    def test_quick_mode_selects_first_available_credential(self, MockStore):
        from skua.commands.add import cmd_add

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_project.return_value = None
        store.load_global.return_value = {"defaults": {}}
        store.load_agent.return_value = self._claude_agent()
        store.load_environment.return_value = None

        creds = {
            "acred": Credential(name="acred", agent="claude"),
            "zcred": Credential(name="zcred", agent="claude"),
        }
        store.list_resources.side_effect = (
            lambda kind: ["claude"] if kind == "AgentConfig" else ["zcred", "acred"]
        )
        store.load_credential.side_effect = lambda name: creds.get(name)

        cmd_add(self._args())

        saved = [c.args[0] for c in store.save_resource.call_args_list]
        self.assertEqual(len(saved), 1)
        self.assertIsInstance(saved[0], Project)
        self.assertEqual(saved[0].credential, "acred")

    @mock.patch("builtins.input", return_value="imported-cred")
    @mock.patch("skua.commands.add.agent_default_source_dir")
    @mock.patch("skua.commands.add.resolve_credential_sources")
    @mock.patch("skua.commands.add.ConfigStore")
    def test_auto_imports_local_credentials_when_none_exist(
        self,
        MockStore,
        mock_sources,
        mock_source_dir,
        _mock_input,
    ):
        from skua.commands.add import cmd_add

        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / ".claude"
            source_dir.mkdir(parents=True)
            auth_file = source_dir / ".credentials.json"
            auth_file.write_text('{"token":"abc"}')

            store = MockStore.return_value
            store.is_initialized.return_value = True
            store.load_project.return_value = None
            store.load_global.return_value = {"defaults": {}}
            store.load_agent.return_value = self._claude_agent()
            store.load_environment.return_value = None
            store.list_resources.side_effect = (
                lambda kind: ["claude"] if kind == "AgentConfig" else []
            )
            store.load_credential.return_value = None

            mock_source_dir.return_value = source_dir
            mock_sources.return_value = [(auth_file, ".credentials.json")]

            cmd_add(self._args())

            saved = [c.args[0] for c in store.save_resource.call_args_list]
            self.assertEqual(len(saved), 2)
            self.assertIsInstance(saved[0], Credential)
            self.assertEqual(saved[0].name, "imported-cred")
            self.assertEqual(saved[0].source_dir, str(source_dir))
            self.assertIsInstance(saved[1], Project)
            self.assertEqual(saved[1].credential, "imported-cred")

    @mock.patch("skua.commands.add.select_option", return_value="Skip credential setup (log in inside the container)")
    @mock.patch("skua.commands.add.find_ssh_keys", return_value=[])
    @mock.patch("builtins.input", return_value="")
    @mock.patch("skua.commands.add.resolve_credential_sources")
    @mock.patch("skua.commands.add.ConfigStore")
    def test_interactive_skip_when_no_credentials_and_no_local_found(
        self, MockStore, mock_sources, _mock_input, _mock_keys, mock_select
    ):
        from skua.commands.add import cmd_add

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_project.return_value = None
        store.load_global.return_value = {"defaults": {}}
        store.load_agent.return_value = self._claude_agent()
        store.load_environment.return_value = None
        store.list_resources.side_effect = (
            lambda kind: ["claude"] if kind == "AgentConfig" else []
        )
        store.load_credential.return_value = None
        mock_sources.return_value = [(Path("/missing/.credentials.json"), ".credentials.json")]

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_add(self._args(quick=False, no_prompt=False, agent="claude"))

        saved = [c.args[0] for c in store.save_resource.call_args_list]
        self.assertEqual(len(saved), 1)
        self.assertIsInstance(saved[0], Project)
        self.assertEqual(saved[0].credential, "")
        self.assertIn("Skipping credential setup", buf.getvalue())

    @mock.patch("skua.commands.add.resolve_credential_sources")
    @mock.patch("skua.commands.add.ConfigStore")
    def test_errors_when_no_credentials_exist_and_no_local_found(self, MockStore, mock_sources):
        from skua.commands.add import cmd_add

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_project.return_value = None
        store.load_global.return_value = {"defaults": {}}
        store.load_agent.return_value = self._claude_agent()
        store.load_environment.return_value = None
        store.list_resources.side_effect = (
            lambda kind: ["claude"] if kind == "AgentConfig" else []
        )
        store.load_credential.return_value = None

        mock_sources.return_value = [(Path("/missing/.credentials.json"), ".credentials.json")]

        with self.assertRaises(SystemExit) as ctx:
            cmd_add(self._args())
        self.assertEqual(ctx.exception.code, 1)
        store.save_resource.assert_not_called()

    @mock.patch("skua.commands.add.ConfigStore")
    def test_no_credential_flag_skips_credential_setup(self, MockStore):
        from skua.commands.add import cmd_add

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_project.return_value = None
        store.load_global.return_value = {"defaults": {}}
        store.load_agent.return_value = self._claude_agent()
        store.load_environment.return_value = None
        store.list_resources.side_effect = (
            lambda kind: ["claude"] if kind == "AgentConfig" else []
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_add(self._args(no_credential=True))

        saved = [c.args[0] for c in store.save_resource.call_args_list]
        self.assertEqual(len(saved), 1)
        self.assertIsInstance(saved[0], Project)
        self.assertEqual(saved[0].credential, "")
        self.assertIn("Skipping credential setup", buf.getvalue())

    @mock.patch("skua.commands.add.select_option")
    @mock.patch("skua.commands.add.find_ssh_keys")
    @mock.patch("skua.commands.add.ConfigStore")
    def test_ssh_selector_includes_none_option(self, MockStore, mock_find_ssh_keys, mock_select):
        from skua.commands.add import cmd_add

        store = MockStore.return_value
        store.is_initialized.return_value = True
        store.load_project.return_value = None
        store.load_global.return_value = {"defaults": {"sshKey": "/tmp/default-key"}}
        store.load_agent.return_value = self._claude_agent()
        store.load_environment.return_value = None
        store.list_resources.side_effect = (
            lambda kind: ["claude"] if kind == "AgentConfig" else []
        )
        store.load_credential.return_value = Credential(name="cred1", agent="claude")

        mock_find_ssh_keys.return_value = [Path("/tmp/id_ed25519")]
        mock_select.return_value = "None"

        cmd_add(
            self._args(
                quick=False,
                no_prompt=False,
                agent="claude",
                credential="cred1",
            )
        )

        saved = [c.args[0] for c in store.save_resource.call_args_list]
        self.assertEqual(len(saved), 1)
        self.assertIsInstance(saved[0], Project)
        self.assertEqual(saved[0].ssh.private_key, "")
        self.assertIn("None", mock_select.call_args.args[1])

    @mock.patch("skua.commands.add.select_option", return_value="zcred")
    def test_interactive_credential_prompt_uses_selector(self, mock_select):
        from skua.commands.add import _select_existing_credential

        selected = _select_existing_credential(["acred", "zcred"], quick=False, no_prompt=False)
        self.assertEqual(selected, "zcred")
        mock_select.assert_called_once_with("Select credential:", ["acred", "zcred"], default_index=0)


class TestListColumns(unittest.TestCase):
    @mock.patch("skua.commands.list_cmd.get_running_skua_containers")
    @mock.patch("skua.commands.list_cmd._has_pending_adapt_request", return_value=True)
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_default_shows_default_columns(self, MockStore, _mock_pending, mock_running):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["demo"]
        store.resolve_project.return_value = Project(
            name="demo",
            directory="/tmp/demo",
            environment="local-docker",
            security="open",
            agent="claude",
            credential="cred-main",
        )
        store.load_environment.return_value = SimpleNamespace(
            network=SimpleNamespace(mode="bridge")
        )
        mock_running.return_value = {"skua-demo"}

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace())
        out = buf.getvalue()

        self.assertIn("NAME", out)
        self.assertIn("ACTIVITY", out)
        self.assertIn("STATUS", out)
        self.assertIn("SOURCE", out)
        self.assertNotIn("AGENT", out)
        self.assertNotIn("CREDENTIAL", out)
        self.assertNotIn("SECURITY", out)
        self.assertNotIn("NETWORK", out)
        self.assertIn("running*", out)
        self.assertIn("1 pending adapt", out)
        self.assertIn("* pending image-request changes", out)

    @mock.patch("skua.commands.list_cmd.get_running_skua_containers")
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_with_agent_and_security_flags_shows_extra_columns(self, MockStore, mock_running):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["demo"]
        store.resolve_project.return_value = Project(
            name="demo",
            directory="/tmp/demo",
            environment="local-docker",
            security="open",
            agent="claude",
            credential="cred-main",
        )
        store.load_environment.return_value = SimpleNamespace(
            network=SimpleNamespace(mode="bridge")
        )
        mock_running.return_value = {"skua-demo"}

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace(agent=True, security=True))
        out = buf.getvalue()

        self.assertIn("ACTIVITY", out)
        self.assertIn("AGENT", out)
        self.assertIn("CREDENTIAL", out)
        self.assertIn("SECURITY", out)
        self.assertIn("NETWORK", out)
        self.assertIn("claude", out)
        self.assertIn("cred-main", out)
        self.assertIn("open", out)
        self.assertIn("bridge", out)

    @mock.patch("skua.commands.list_cmd.image_exists", return_value=False)
    @mock.patch("skua.commands.list_cmd.get_running_skua_containers")
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_checks_remote_host_status(self, MockStore, mock_running, _mock_image_exists):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["local", "qar"]

        projects = {
            "local": Project(
                name="local",
                directory="/tmp/local",
                environment="local-docker",
                security="open",
                agent="claude",
            ),
            "qar": Project(
                name="qar",
                repo="git@github.com:org/repo.git",
                host="qar",
                environment="local-docker",
                security="open",
                agent="claude",
            ),
        }
        store.resolve_project.side_effect = lambda name: projects[name]
        store.load_environment.return_value = SimpleNamespace(
            network=SimpleNamespace(mode="bridge")
        )

        mock_running.side_effect = [
            [],
            ["skua-qar"],
        ]

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace())
        out = buf.getvalue()

        self.assertIn("SSH:qar", out)
        self.assertIn("running", out)
        self.assertIn("2 project(s), 1 running", out)
        self.assertEqual(2, mock_running.call_count)
        mock_running.assert_any_call()
        mock_running.assert_any_call(host="qar")
    @mock.patch("skua.commands.list_cmd.image_exists")
    @mock.patch("skua.commands.list_cmd.get_running_skua_containers", return_value=[])
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_status_reflects_image_existence(self, MockStore, _mock_running, mock_image_exists):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["built-proj", "missing-proj"]

        projects = {
            "built-proj": Project(name="built-proj", directory="/tmp/bp", agent="claude"),
            "missing-proj": Project(name="missing-proj", directory="/tmp/mp", agent="claude"),
        }
        store.resolve_project.side_effect = lambda name: projects[name]
        store.load_environment.return_value = SimpleNamespace(network=SimpleNamespace(mode="bridge"))

        # built-proj has image, missing-proj does not
        mock_image_exists.side_effect = lambda name: "built-proj" in str(name)

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace())
        out = buf.getvalue()

        self.assertIn("built", out)
        self.assertIn("missing", out)
        self.assertNotIn("stopped", out)

    @mock.patch("skua.commands.list_cmd.get_running_skua_containers")
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_caches_remote_host_status_per_host(self, MockStore, mock_running):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["qar-a", "qar-b"]

        def _project(name):
            return Project(
                name=name,
                repo="git@github.com:org/repo.git",
                host="qar",
                environment="local-docker",
                security="open",
                agent="claude",
            )

        store.resolve_project.side_effect = lambda name: _project(name)
        store.load_environment.return_value = SimpleNamespace(
            network=SimpleNamespace(mode="bridge")
        )

        mock_running.side_effect = [
            [],
            ["skua-qar-a", "skua-qar-b"],
        ]

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace())
        out = buf.getvalue()

        self.assertIn("2 project(s), 2 running", out)
        self.assertEqual(2, mock_running.call_count)
        mock_running.assert_any_call()
        mock_running.assert_any_call(host="qar")

    @mock.patch("skua.commands.list_cmd.image_exists", return_value=True)
    @mock.patch("skua.commands.list_cmd.get_running_skua_containers")
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_shows_unreachable_when_ssh_host_fails(self, MockStore, mock_running, _mock_image_exists):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["remote"]
        store.resolve_project.return_value = Project(
            name="remote",
            repo="git@github.com:org/repo.git",
            host="badhost",
            environment="local-docker",
            security="open",
            agent="claude",
        )
        store.load_environment.return_value = SimpleNamespace(
            network=SimpleNamespace(mode="bridge")
        )

        # Local query succeeds; SSH to "badhost" fails (returns None)
        mock_running.side_effect = [[], None]

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace())
        out = buf.getvalue()

        self.assertIn("unreachable", out)
        self.assertNotIn("built", out)
        self.assertIn("1 project(s), 0 running", out)

    @mock.patch("skua.commands.list_cmd.get_running_skua_containers", return_value=[])
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_truncates_long_source_to_column_width(self, MockStore, _mock_running):
        from skua.commands.list_cmd import cmd_list

        long_path = "/very/deeply/nested/directory/structure/that/is/way/too/long/for/the/column"
        store = MockStore.return_value
        store.load_global.return_value = {}
        store.list_resources.return_value = ["proj"]
        store.resolve_project.return_value = Project(
            name="proj",
            directory=long_path,
            environment="local-docker",
            security="open",
            agent="claude",
        )
        store.load_environment.return_value = SimpleNamespace(
            network=SimpleNamespace(mode="bridge")
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_list(argparse.Namespace())
        out = buf.getvalue()

        # Every data line must have the SOURCE column starting at the right offset.
        source_col_width = 38
        for line in out.splitlines():
            if "proj" in line and ("missing" in line or "built" in line):
                # NAME(16) + space + ACTIVITY(14) + space + STATUS(12) + space = offset 45.
                # Verify the SOURCE field does not bleed: its slice must be exactly 38 chars
                source_start = 45
                source_field = line[source_start:source_start + source_col_width]
                self.assertEqual(len(source_field), source_col_width,
                                 f"SOURCE field should be exactly {source_col_width} chars")


class TestAgentActivityFormatting(unittest.TestCase):
    @mock.patch("skua.commands.list_cmd.subprocess.run")
    def test_api_activity_below_threshold_returns_idle(self, mock_run):
        from skua.commands.list_cmd import _agent_activity

        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"state":"api_activity","hits":99,"window":30,"ts":123}\n',
        )

        self.assertEqual("idle", _agent_activity("skua-demo"))

    @mock.patch("skua.commands.list_cmd.subprocess.run")
    def test_api_activity_renders_bar(self, mock_run):
        from skua.commands.list_cmd import _agent_activity

        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"state":"api_activity","hits":250,"window":30,"ts":123}\n',
        )

        self.assertEqual("XX", _agent_activity("skua-demo"))

    @mock.patch("skua.commands.list_cmd.subprocess.run")
    def test_api_activity_caps_bar_width(self, mock_run):
        from skua.commands.list_cmd import _agent_activity

        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"state":"api_activity","hits":1000,"window":30,"ts":123}\n',
        )

        self.assertEqual("XXXXXX", _agent_activity("skua-demo"))

    @mock.patch("skua.commands.list_cmd.subprocess.run")
    def test_processing_state_has_priority(self, mock_run):
        from skua.commands.list_cmd import _agent_activity

        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='{"state":"processing","ts":123}\n',
        )

        self.assertEqual("processing", _agent_activity("skua-demo"))


class TestGitStatusMonitoring(unittest.TestCase):
    def test_git_status_monitors_bind_directory_when_git_repo(self):
        from skua.commands import list_cmd

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir)
            (repo_dir / ".git").mkdir()
            project = Project(name="demo", directory=str(repo_dir), repo="", host="")
            store = mock.Mock()

            with mock.patch.object(list_cmd.subprocess, "run") as mock_run:
                mock_run.side_effect = [
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                    SimpleNamespace(returncode=0, stdout="", stderr=""),
                    SimpleNamespace(returncode=0, stdout="0 0\n", stderr=""),
                ]

                status = list_cmd._git_status(project, store)

        self.assertEqual("CURRENT", status)
        self.assertEqual(3, mock_run.call_count)

    def test_git_status_skips_bind_directory_when_not_git_repo(self):
        from skua.commands import list_cmd

        with tempfile.TemporaryDirectory() as tmpdir:
            project = Project(name="demo", directory=tmpdir, repo="", host="")
            store = mock.Mock()
            with mock.patch.object(list_cmd.subprocess, "run") as mock_run:
                status = list_cmd._git_status(project, store)

        self.assertEqual("", status)
        mock_run.assert_not_called()
