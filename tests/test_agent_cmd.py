# SPDX-License-Identifier: BUSL-1.1
"""Tests for `skua agent` CLI command."""

import argparse
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.loader import ConfigStore
from skua.config.resources import AgentConfig, Credential, Project


def _make_store(tmpdir: str) -> ConfigStore:
    store = ConfigStore(config_dir=Path(tmpdir))
    store.ensure_dirs()
    store.save_global({"defaults": {"agent": "claude"}})
    store.save_resource(AgentConfig(name="claude"))
    store.save_resource(AgentConfig(name="codex"))
    store.save_resource(Credential(name="claude-cred", agent="claude"))
    store.save_resource(Credential(name="generic-cred", agent=""))
    return store


class TestAgentCmdSet(unittest.TestCase):
    def test_set_changes_agent_and_clears_incompatible_credential(self):
        from skua.commands.agent_cmd import cmd_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            store.save_resource(
                Project(name="alpha", directory="/src/alpha",
                        agent="claude", credential="claude-cred")
            )

            args = argparse.Namespace(action="set", name="alpha",
                                       agent="codex", keep_credential=False)
            with mock.patch("skua.commands.agent_cmd.ConfigStore", return_value=store):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cmd_agent(args)
                output = buf.getvalue()

            updated = store.load_project("alpha")
        self.assertEqual("codex", updated.agent)
        self.assertEqual("", updated.credential)
        self.assertIn("claude -> codex", output)
        self.assertIn("claude-cred", output)

    def test_set_keeps_credential_when_compatible(self):
        from skua.commands.agent_cmd import cmd_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            store.save_resource(
                Project(name="alpha", directory="/src/alpha",
                        agent="claude", credential="generic-cred")
            )

            args = argparse.Namespace(action="set", name="alpha",
                                       agent="codex", keep_credential=False)
            with mock.patch("skua.commands.agent_cmd.ConfigStore", return_value=store):
                with redirect_stdout(io.StringIO()):
                    cmd_agent(args)

            updated = store.load_project("alpha")
        self.assertEqual("codex", updated.agent)
        self.assertEqual("generic-cred", updated.credential)

    def test_set_keep_credential_flag_overrides_clear(self):
        from skua.commands.agent_cmd import cmd_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            store.save_resource(
                Project(name="alpha", directory="/src/alpha",
                        agent="claude", credential="claude-cred")
            )

            args = argparse.Namespace(action="set", name="alpha",
                                       agent="codex", keep_credential=True)
            with mock.patch("skua.commands.agent_cmd.ConfigStore", return_value=store):
                with redirect_stdout(io.StringIO()):
                    cmd_agent(args)

            updated = store.load_project("alpha")
        self.assertEqual("codex", updated.agent)
        self.assertEqual("claude-cred", updated.credential)

    def test_set_unknown_agent_exits_nonzero(self):
        from skua.commands.agent_cmd import cmd_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            store.save_resource(Project(name="alpha", agent="claude"))

            args = argparse.Namespace(action="set", name="alpha",
                                       agent="ghost", keep_credential=False)
            with mock.patch("skua.commands.agent_cmd.ConfigStore", return_value=store):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        cmd_agent(args)
        self.assertEqual(1, ctx.exception.code)

    def test_set_unknown_project_exits_nonzero(self):
        from skua.commands.agent_cmd import cmd_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)

            args = argparse.Namespace(action="set", name="ghost",
                                       agent="codex", keep_credential=False)
            with mock.patch("skua.commands.agent_cmd.ConfigStore", return_value=store):
                with redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as ctx:
                        cmd_agent(args)
        self.assertEqual(1, ctx.exception.code)


class TestAgentCmdList(unittest.TestCase):
    def test_list_marks_default_agent(self):
        from skua.commands.agent_cmd import cmd_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            args = argparse.Namespace(action="list")
            with mock.patch("skua.commands.agent_cmd.ConfigStore", return_value=store):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    cmd_agent(args)
                output = buf.getvalue()
        self.assertIn("claude (default)", output)
        self.assertIn("codex", output)
        self.assertNotIn("codex (default)", output)


class TestDashboardAgentEditClearsCredential(unittest.TestCase):
    """Selecting a new agent from the project detail view should drop a
    credential that was tied to the previous agent."""

    def test_dashboard_agent_select_clears_incompatible_credential(self):
        from skua.commands.add import _cred_matches_agent

        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            project = Project(name="alpha", directory="/src/alpha",
                              agent="claude", credential="claude-cred")

            # Simulate the dashboard's select-edit commit logic.
            project.agent = "codex"
            cred_name = project.credential
            if cred_name and not _cred_matches_agent(store, cred_name, project.agent):
                project.credential = ""

        self.assertEqual("codex", project.agent)
        self.assertEqual("", project.credential)


class TestDashboardCredentialSelectFilters(unittest.TestCase):
    """The dashboard credential picker should hide credentials tied to a
    different agent than the project's current one."""

    def _filter(self, store, agent_name: str) -> list:
        # Mirrors the dashboard's filter in `detail_edit_select` for credential.
        from skua.commands.add import _cred_matches_agent
        names = store.list_resources("Credential")
        return [c for c in names if _cred_matches_agent(store, c, agent_name)]

    def test_credential_options_filtered_by_project_agent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _make_store(tmpdir)
            store.save_resource(Credential(name="codex-cred", agent="codex"))

            for_claude = self._filter(store, "claude")
            for_codex = self._filter(store, "codex")

        # generic-cred has no agent — compatible with both.
        self.assertIn("claude-cred", for_claude)
        self.assertIn("generic-cred", for_claude)
        self.assertNotIn("codex-cred", for_claude)

        self.assertIn("codex-cred", for_codex)
        self.assertIn("generic-cred", for_codex)
        self.assertNotIn("claude-cred", for_codex)


if __name__ == "__main__":
    unittest.main()
