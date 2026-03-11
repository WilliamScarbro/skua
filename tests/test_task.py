# SPDX-License-Identifier: BUSL-1.1
"""Tests for task plan parsing and dispatch helpers."""

import argparse
import io
import importlib.util
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
import sys
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.config.resources import AgentConfig, AgentRuntimeSpec, Project


class TestTaskPlan(unittest.TestCase):
    def test_public_import_surface_exposes_task_api(self):
        from skua import TaskPlan, dispatch_task_plan, load_task_brief, make_task_plan, run_task_prompt

        self.assertIsNotNone(TaskPlan)
        self.assertTrue(callable(load_task_brief))
        self.assertTrue(callable(make_task_plan))
        self.assertTrue(callable(dispatch_task_plan))
        self.assertTrue(callable(run_task_prompt))

    def test_make_task_plan_supports_explicit_python_order(self):
        from skua import load_task_brief, make_task_plan

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            schema = root / "03-schema-migration-agent.md"
            repo = root / "01-repo-worktree-agent.md"
            schema.write_text("# Schema Migration\n\n## Objective\n\nHandle schema.\n", encoding="utf-8")
            repo.write_text("# Repo Worktree\n\n## Objective\n\nHandle repos.\n", encoding="utf-8")

            schema_brief = load_task_brief(schema)
            repo_brief = load_task_brief(repo, depends_on=[schema_brief.brief_file])
            plan = make_task_plan(
                tasks=[schema_brief, repo_brief],
                suggested_order=[schema_brief.brief_file, repo_brief.brief_file],
            )

        self.assertEqual([task.brief_file for task in plan.tasks], [schema_brief.brief_file, repo_brief.brief_file])
        self.assertEqual(plan.tasks[1].depends_on, [schema_brief.brief_file])

    def test_load_task_plan_uses_readme_order(self):
        from skua import load_task_plan

        with tempfile.TemporaryDirectory() as tmpdir:
            plan_dir = Path(tmpdir)
            (plan_dir / "README.md").write_text(
                "# Plan\n\n"
                "## Suggested execution order\n\n"
                "1. `03-schema-migration-agent.md`\n"
                "2. `01-repo-worktree-agent.md`\n",
                encoding="utf-8",
            )
            (plan_dir / "01-repo-worktree-agent.md").write_text(
                "# Repo Worktree\n\n## Objective\n\nHandle repos.\n",
                encoding="utf-8",
            )
            (plan_dir / "03-schema-migration-agent.md").write_text(
                "# Schema Migration\n\n## Objective\n\nHandle schema.\n",
                encoding="utf-8",
            )

            plan = load_task_plan(plan_dir)

        self.assertEqual(
            [task.brief_file for task in plan.tasks],
            ["03-schema-migration-agent.md", "01-repo-worktree-agent.md"],
        )
        self.assertEqual(plan.tasks[0].depends_on, [])
        self.assertEqual(plan.tasks[1].depends_on, ["03-schema-migration-agent.md"])
        self.assertEqual(plan.tasks[0].slug, "schema-migration-agent")

    def test_recovered_image_refactor_process_loads_explicit_plan(self):
        process_path = Path("/home/dev/image_refactor_plan/process.py")
        spec = importlib.util.spec_from_file_location("image_refactor_process", process_path)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        plan = module.build_image_refactor_plan()

        self.assertEqual(
            [task.brief_file for task in plan.tasks],
            [
                "03-schema-migration-agent.md",
                "01-repo-worktree-agent.md",
                "02-image-identity-gc-agent.md",
                "04-cli-ux-agent.md",
                "05-test-migration-agent.md",
            ],
        )
        self.assertEqual(plan.tasks[1].depends_on, ["03-schema-migration-agent.md"])
        self.assertEqual(
            plan.tasks[2].depends_on,
            ["03-schema-migration-agent.md", "01-repo-worktree-agent.md"],
        )
        self.assertEqual(
            plan.tasks[4].depends_on,
            [
                "03-schema-migration-agent.md",
                "01-repo-worktree-agent.md",
                "02-image-identity-gc-agent.md",
                "04-cli-ux-agent.md",
            ],
        )
        self.assertEqual(Path(plan.readme_path), Path("/home/dev/image_refactor_plan/README.md"))


class TestPromptCommand(unittest.TestCase):
    def test_build_agent_prompt_command_prefers_prompt_command(self):
        from skua import build_agent_prompt_command

        agent = AgentConfig(
            name="codex",
            runtime=AgentRuntimeSpec(
                command="codex",
                prompt_command="codex exec {prompt_shell}",
            ),
        )

        cmd = build_agent_prompt_command(agent, "demo", "Solve it")

        self.assertEqual(cmd[:3], ["codex", "exec", "--skip-git-repo-check"])
        self.assertEqual(cmd[-1], "Solve it")

    @mock.patch("skua.tasks.is_container_running", return_value=True)
    @mock.patch("skua.tasks.ConfigStore")
    def test_run_task_prompt_dry_run_builds_docker_exec(self, MockStore, _mock_running):
        from skua import run_task_prompt

        store = MockStore.return_value
        store.resolve_project.return_value = Project(name="demo", directory="/tmp/demo", agent="codex")
        store.load_agent.return_value = AgentConfig(
            name="codex",
            runtime=AgentRuntimeSpec(command="codex"),
        )

        result = run_task_prompt(
            project_name="demo",
            prompt="Review file layout",
            title="repo-task",
            background=True,
            dry_run=True,
        )

        self.assertEqual(result.project, "demo")
        self.assertEqual(result.container, "skua-demo")
        self.assertIn("/tmp/skua-task-repo-task.log", result.command[-1])
        self.assertEqual(result.log_path, "/tmp/skua-task-repo-task.log")


class TestTaskDispatch(unittest.TestCase):
    @staticmethod
    def _args(**kwargs):
        defaults = dict(
            action="dispatch",
            plan_dir="/tmp/plan",
            project=[],
            project_prefix="refactor-",
            execute=False,
            ensure_running=False,
            background=False,
            dry_run=False,
        )
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    @mock.patch("skua.commands.task.dispatch_task_plan")
    def test_dispatch_prints_project_mapping(self, mock_plan):
        from skua.commands.task import cmd_task_dispatch

        from skua import TaskBrief, TaskMapping

        mock_plan.return_value = (
            [
                TaskMapping(task=TaskBrief(id="a", slug="schema", title="Schema", brief_file="03-schema.md", brief_path="/tmp/03-schema.md"), project="refactor-schema"),
                TaskMapping(task=TaskBrief(id="b", slug="cli", title="CLI", brief_file="04-cli.md", brief_path="/tmp/04-cli.md", depends_on=["03-schema.md"]), project="refactor-cli"),
            ],
            [],
        )

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_task_dispatch(self._args())

        out = buf.getvalue()
        self.assertIn("03-schema.md -> refactor-schema", out)
        self.assertIn("04-cli.md -> refactor-cli", out)
