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
                login_command="claude login",
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
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_default_shows_minimal_columns(self, MockStore, mock_running):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
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
        self.assertIn("SOURCE", out)
        self.assertIn("STATUS", out)
        self.assertNotIn("AGENT", out)
        self.assertNotIn("CREDENTIAL", out)
        self.assertNotIn("SECURITY", out)
        self.assertNotIn("NETWORK", out)

    @mock.patch("skua.commands.list_cmd.get_running_skua_containers")
    @mock.patch("skua.commands.list_cmd.ConfigStore")
    def test_list_with_agent_and_security_flags_shows_extra_columns(self, MockStore, mock_running):
        from skua.commands.list_cmd import cmd_list

        store = MockStore.return_value
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

        self.assertIn("AGENT", out)
        self.assertIn("CREDENTIAL", out)
        self.assertIn("SECURITY", out)
        self.assertIn("NETWORK", out)
        self.assertIn("claude", out)
        self.assertIn("cred-main", out)
        self.assertIn("open", out)
        self.assertIn("bridge", out)
