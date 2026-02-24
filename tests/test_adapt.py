# SPDX-License-Identifier: BUSL-1.1
"""Tests for skua adapt workflow and image-request helpers."""

import argparse
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from skua.commands.adapt import cmd_adapt
from skua.config.loader import ConfigStore
from skua.config.resources import AgentAuthSpec, AgentConfig, AgentRuntimeSpec, Credential, Environment, Project, SecurityProfile
from skua.project_adapt import (
    ensure_adapt_workspace,
    load_image_request,
    request_has_updates,
    apply_image_request_to_project,
    smoke_test_path,
)


class TestProjectAdaptHelpers(unittest.TestCase):
    def test_ensure_workspace_creates_guide_and_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "proj"
            project_dir.mkdir()
            guide, request = ensure_adapt_workspace(project_dir, "proj", "codex")
            self.assertTrue(guide.is_file())
            self.assertTrue(request.is_file())
            self.assertTrue((project_dir / "AGENTS.md").is_file())
            self.assertTrue((project_dir / "CLAUDE.md").is_file())
            self.assertIn("Skua Image Adapt", guide.read_text())
            self.assertIn("schemaVersion: 1", request.read_text())
            agents_text = (project_dir / "AGENTS.md").read_text()
            self.assertIn(".skua/image-request.yaml", agents_text)
            self.assertNotIn("skua adapt", agents_text)
            self.assertNotIn("Dockerfile", agents_text)

    def test_request_has_updates_and_apply_to_project(self):
        project = Project(name="p1")
        request = {
            "baseImage": "debian:stable-slim",
            "packages": ["libpq-dev"],
            "commands": ["npm ci"],
        }
        self.assertTrue(request_has_updates(request))
        changed = apply_image_request_to_project(project, request)
        self.assertTrue(changed)
        self.assertEqual(project.image.base_image, "debian:stable-slim")
        self.assertEqual(project.image.extra_packages, ["libpq-dev"])
        self.assertEqual(project.image.extra_commands, ["npm ci"])
        self.assertEqual(project.image.version, 1)

    def test_sync_auth_from_host_overwrites_stale_project_auth(self):
        from skua.commands.adapt import _sync_auth_from_host

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            src_dir = tmp / "src-auth"
            src_dir.mkdir(parents=True, exist_ok=True)
            (src_dir / "auth.json").write_text('{"token":"fresh"}')

            data_dir = tmp / "project-auth"
            data_dir.mkdir(parents=True, exist_ok=True)
            (data_dir / "auth.json").write_text('{"token":"stale"}')

            agent = AgentConfig(
                name="codex",
                auth=AgentAuthSpec(dir=".codex", files=["auth.json"]),
            )
            cred = Credential(name="cred", agent="codex", source_dir=str(src_dir))

            copied = _sync_auth_from_host(data_dir=data_dir, cred=cred, agent=agent)
            self.assertEqual(1, copied)
            self.assertEqual('{"token":"fresh"}', (data_dir / "auth.json").read_text())

    def test_summarize_agent_output_filters_entrypoint_noise(self):
        from skua.commands.adapt import _summarize_agent_output

        stdout = "\n".join([
            "============================================",
            "Agent: claude",
            "Auth:  .claude",
            "Usage:",
            "  claude -> Start claude",
            "Updated .skua/image-request.yaml",
        ])
        stderr = ""

        summary = _summarize_agent_output(stdout, stderr)
        self.assertEqual(["Updated .skua/image-request.yaml"], summary)

    def test_request_preview_lines_formats_key_fields(self):
        from skua.commands.adapt import _request_preview_lines

        request = {
            "summary": "Need pkg setup",
            "fromImage": "",
            "baseImage": "debian:bookworm-slim",
            "packages": ["git", "jq"],
            "commands": ["npm ci"],
        }
        lines = _request_preview_lines(request)
        self.assertIn("summary: Need pkg setup", lines)
        self.assertIn("fromImage: (unchanged)", lines)
        self.assertIn("baseImage: debian:bookworm-slim", lines)
        self.assertIn("packages: git, jq", lines)
        self.assertIn("command: npm ci", lines)
        self.assertNotIn("commands: 1 command(s)", lines)

    def test_agent_prompt_describes_container_and_project_file_inference(self):
        from skua.commands.adapt import _agent_prompt

        prompt = _agent_prompt("proj", "claude")
        self.assertIn("running inside the project's Docker container environment", prompt)
        self.assertIn("Infer dependencies by reading project files", prompt)
        self.assertIn("missing tool/system dependency blocks progress", prompt)

    def test_agent_prompt_includes_build_error_context(self):
        from skua.commands.adapt import _agent_prompt, _format_build_error_context

        dockerfile_text = "FROM debian:bookworm-slim\nRUN echo ok\n"
        error_output = "RUN bad-command: not found"
        build_error = _format_build_error_context(error_output, dockerfile_text)
        prompt = _agent_prompt("proj", "claude", build_error=build_error)
        self.assertIn("Dockerfile used for build", prompt)
        self.assertIn("Build error output", prompt)
        self.assertIn("RUN bad-command", prompt)

    def test_project_has_pending_request_detects_unapplied_changes(self):
        from skua.commands.adapt import _project_has_pending_request

        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "proj"
            project_dir.mkdir()
            project = Project(name="proj", directory=str(project_dir))
            ensure_adapt_workspace(project_dir, "proj", "codex")
            (project_dir / ".skua" / "image-request.yaml").write_text(
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "packages": ["git"],
                    },
                    default_flow_style=False,
                    sort_keys=False,
                )
            )
            self.assertTrue(_project_has_pending_request(project))


class TestAdaptCommand(unittest.TestCase):
    def _new_store(self, config_dir: Path) -> ConfigStore:
        store = ConfigStore(config_dir=config_dir)
        store.ensure_dirs()
        store.save_global({"defaults": {"environment": "local-docker", "security": "open", "agent": "codex"}})
        store.save_resource(Environment(name="local-docker"))
        store.save_resource(SecurityProfile(name="open"))
        store.save_resource(AgentConfig(name="codex"))
        return store

    def _write_project_files(self, base_dir: Path, files: dict[str, str]) -> Path:
        base_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in files.items():
            p = base_dir / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        return base_dir

    def _adapt_args(
        self,
        name: str,
        apply_only: bool = False,
        build: bool = False,
        show_prompt: bool = False,
        discover: bool = False,
        force: bool = False,
    ):
        return argparse.Namespace(
            name=name,
            show_prompt=show_prompt,
            discover=discover,
            base_image="",
            from_image="",
            package=[],
            extra_command=[],
            apply_only=apply_only,
            clear=False,
            write_only=False,
            build=build,
            force=force,
        )

    def test_cmd_adapt_show_prompt_prints_and_exits_early(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            stdout = io.StringIO()
            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session") as mock_session,
                mock.patch("sys.stdout", stdout),
            ):
                cmd_adapt(self._adapt_args("proj", show_prompt=True))

            out = stdout.getvalue()
            self.assertIn("Adapt prompt for project 'proj' (agent: codex):", out)
            self.assertIn("Resolved non-interactive agent command:", out)
            self.assertIn("codex exec", out)
            mock_session.assert_not_called()

    def test_cmd_adapt_applies_request_to_project_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            guide, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            self.assertTrue(guide.exists())
            with open(request_path, "w") as f:
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "fromImage": "ghcr.io/acme/myapp:latest",
                        "packages": ["git", "jq"],
                        "commands": ["echo prepared"],
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

            args = self._adapt_args("proj", apply_only=True)
            with mock.patch("skua.commands.adapt.ConfigStore", return_value=store):
                cmd_adapt(args)

            updated = store.load_project("proj")
            self.assertEqual(updated.image.from_image, "ghcr.io/acme/myapp:latest")
            self.assertEqual(updated.image.extra_packages, ["git", "jq"])
            self.assertEqual(updated.image.extra_commands, ["echo prepared"])
            self.assertEqual(updated.image.version, 1)

            applied = load_image_request(request_path)
            self.assertEqual(applied["status"], "applied")

    def test_cmd_adapt_applies_expected_changes_for_multiple_fixture_projects(self):
        fixtures = [
            {
                "name": "py-analytics",
                "files": {
                    "requirements.txt": "fastapi==0.116.0\npsycopg[binary]==3.2.9\n",
                    "app/main.py": "print('ready')\n",
                },
                "request": {
                    "schemaVersion": 1,
                    "status": "ready",
                    "summary": "Install libpq for psycopg and bootstrap Python deps.",
                    "baseImage": "python:3.12-slim-bookworm",
                    "packages": ["libpq-dev"],
                    "commands": ["pip install -r requirements.txt"],
                },
                "expected": {
                    "from_image": "",
                    "base_image": "python:3.12-slim-bookworm",
                    "extra_packages": ["libpq-dev"],
                    "extra_commands": ["pip install -r requirements.txt"],
                },
            },
            {
                "name": "node-dashboard",
                "files": {
                    "package.json": (
                        "{\n"
                        '  "name": "node-dashboard",\n'
                        '  "private": true,\n'
                        '  "dependencies": { "esbuild": "^0.25.0", "sqlite3": "^5.1.7" }\n'
                        "}\n"
                    ),
                    "src/index.js": "console.log('ready')\n",
                },
                "request": {
                    "schemaVersion": 1,
                    "status": "ready",
                    "summary": "Use node image and build native npm modules.",
                    "fromImage": "node:22-bookworm-slim",
                    "packages": ["python3", "g++", "make"],
                    "commands": ["npm ci"],
                },
                "expected": {
                    "from_image": "node:22-bookworm-slim",
                    "base_image": "",
                    "extra_packages": ["python3", "g++", "make"],
                    "extra_commands": ["npm ci"],
                },
            },
            {
                "name": "go-worker",
                "files": {
                    "go.mod": (
                        "module example.com/go-worker\n\n"
                        "go 1.23.0\n\n"
                        "require github.com/jackc/pgx/v5 v5.6.0\n"
                    ),
                    "cmd/worker/main.go": (
                        "package main\n\n"
                        "func main() {}\n"
                    ),
                },
                "request": {
                    "schemaVersion": 1,
                    "status": "ready",
                    "summary": "Start from go toolchain image and prefetch modules.",
                    "fromImage": "golang:1.23-bookworm",
                    "packages": ["git", "ca-certificates"],
                    "commands": ["go mod download"],
                },
                "expected": {
                    "from_image": "golang:1.23-bookworm",
                    "base_image": "",
                    "extra_packages": ["git", "ca-certificates"],
                    "extra_commands": ["go mod download"],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._new_store(tmp / "cfg")
            with mock.patch("skua.commands.adapt.ConfigStore", return_value=store):
                for fixture in fixtures:
                    with self.subTest(project=fixture["name"]):
                        project_dir = self._write_project_files(tmp / fixture["name"], fixture["files"])
                        store.save_resource(
                            Project(name=fixture["name"], directory=str(project_dir), agent="codex")
                        )
                        _, request_path = ensure_adapt_workspace(project_dir, fixture["name"], "codex")
                        with open(request_path, "w") as f:
                            yaml.dump(
                                fixture["request"],
                                f,
                                default_flow_style=False,
                                sort_keys=False,
                            )

                        stdout = io.StringIO()
                        with mock.patch("sys.stdout", stdout):
                            cmd_adapt(self._adapt_args(fixture["name"], apply_only=True))
                        output = stdout.getvalue()

                        updated = store.load_project(fixture["name"])
                        self.assertEqual(updated.image.from_image, fixture["expected"]["from_image"])
                        self.assertEqual(updated.image.base_image, fixture["expected"]["base_image"])
                        self.assertEqual(updated.image.extra_packages, fixture["expected"]["extra_packages"])
                        self.assertEqual(updated.image.extra_commands, fixture["expected"]["extra_commands"])
                        self.assertEqual(updated.image.version, 1)

                        applied = load_image_request(request_path)
                        self.assertEqual(applied["status"], "applied")
                        with open(request_path) as f:
                            raw_applied = yaml.safe_load(f) or {}
                        self.assertEqual(raw_applied.get("appliedVersion"), 1)

                        self.assertIn("Applied image request from:", output)
                        self.assertIn("Resolved image config:", output)
                        if fixture["expected"]["from_image"]:
                            self.assertIn(fixture["expected"]["from_image"], output)
                        if fixture["expected"]["base_image"]:
                            self.assertIn(fixture["expected"]["base_image"], output)
                        for pkg in fixture["expected"]["extra_packages"]:
                            self.assertIn(pkg, output)

    def test_cmd_adapt_does_not_run_agent_session_by_default(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "packages": ["git"],
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session") as mock_session,
                mock.patch("skua.commands.adapt._build_project_image") as mock_build,
            ):
                cmd_adapt(self._adapt_args("proj"))

            mock_session.assert_not_called()
            mock_build.assert_not_called()

    def test_cmd_adapt_runs_agent_session_in_discover_mode(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "packages": ["git"],
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session") as mock_session,
                mock.patch("skua.commands.adapt._build_project_image", return_value="") as mock_build,
            ):
                cmd_adapt(self._adapt_args("proj", discover=True))

            # First call: discover wishlist; second call: create smoke test (no smoke test file exists)
            self.assertGreaterEqual(mock_session.call_count, 1)
            first_call_kwargs = mock_session.call_args_list[0].kwargs
            self.assertFalse(first_call_kwargs.get("prompt_override"))  # discover call has no prompt_override
            mock_build.assert_called_once()

    def test_cmd_adapt_discover_retries_build_after_agent_revises_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump(
                    {"schemaVersion": 1, "status": "ready", "packages": ["git"]},
                    f, default_flow_style=False, sort_keys=False,
                )

            # Simulate: first build fails, agent updates request, second build succeeds
            build_calls = []
            def fake_build(store, project, agent):
                build_calls.append(len(build_calls))
                if len(build_calls) == 1:
                    return "RUN bad-command: not found"
                return ""

            session_calls = []
            def fake_session(store, project, env, sec, agent, build_error="", smoke_error="",
                             prompt_override="", warn_on_failure=False):
                session_calls.append({"build_error": build_error, "prompt_override": prompt_override})
                if prompt_override:
                    return  # smoke test creation attempt, don't update request
                if build_error:
                    packages = ["git", "make", "cmake"]
                else:
                    packages = ["git", "make"]
                with open(request_path, "w") as f:
                    yaml.dump(
                        {"schemaVersion": 1, "status": "ready", "packages": packages},
                        f, default_flow_style=False, sort_keys=False,
                    )

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session", side_effect=fake_session),
                mock.patch("skua.commands.adapt._build_project_image", side_effect=fake_build),
            ):
                cmd_adapt(self._adapt_args("proj", discover=True))

            # Discover sessions (non-prompt_override): initial discover + build-error retry
            adapt_sessions = [c for c in session_calls if not c["prompt_override"]]
            self.assertEqual(2, len(adapt_sessions))
            self.assertEqual("", adapt_sessions[0]["build_error"])
            self.assertIn("not found", adapt_sessions[1]["build_error"])
            # Build attempted twice
            self.assertEqual(2, len(build_calls))
            # Revised request was applied (packages include 'make')
            updated = store.load_project("proj")
            self.assertIn("make", updated.image.extra_packages)

    def test_cmd_adapt_build_retries_after_agent_revises_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump(
                    {"schemaVersion": 1, "status": "ready", "packages": ["git"]},
                    f, default_flow_style=False, sort_keys=False,
                )

            build_calls = []
            def fake_build(store, project, agent):
                build_calls.append(len(build_calls))
                if len(build_calls) == 1:
                    return "RUN bad-command: not found"
                return ""

            session_calls = []
            def fake_session(store, project, env, sec, agent, build_error="", smoke_error="",
                             prompt_override="", warn_on_failure=False):
                session_calls.append({"build_error": build_error, "prompt_override": prompt_override})
                if prompt_override:
                    return  # smoke test creation attempt, don't update request
                packages = ["git", "make", "cmake"]
                with open(request_path, "w") as f:
                    yaml.dump(
                        {"schemaVersion": 1, "status": "ready", "packages": packages},
                        f, default_flow_style=False, sort_keys=False,
                    )

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session", side_effect=fake_session),
                mock.patch("skua.commands.adapt._build_project_image", side_effect=fake_build),
            ):
                cmd_adapt(self._adapt_args("proj", build=True))

            # Only the build-error retry session should have updated the request
            retry_sessions = [c for c in session_calls if not c["prompt_override"]]
            self.assertEqual(1, len(retry_sessions))
            self.assertIn("not found", retry_sessions[0]["build_error"])
            self.assertEqual(2, len(build_calls))
            updated = store.load_project("proj")
            self.assertIn("cmake", updated.image.extra_packages)

    def test_cmd_adapt_respects_user_rejection_before_apply(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump(
                    {
                        "schemaVersion": 1,
                        "status": "ready",
                        "packages": ["git"],
                    },
                    f,
                    default_flow_style=False,
                    sort_keys=False,
                )

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._is_interactive_tty", return_value=True),
                mock.patch("skua.commands.adapt._confirm_apply_wishlist", return_value=False) as mock_confirm,
                mock.patch("skua.commands.adapt._run_agent_adapt_session"),
            ):
                cmd_adapt(self._adapt_args("proj", apply_only=True))

            mock_confirm.assert_called_once()
            updated = store.load_project("proj")
            self.assertEqual(updated.image.version, 0)

    def test_cmd_adapt_rejects_discover_with_apply_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_adapt(self._adapt_args("proj", apply_only=True, discover=True))
            self.assertEqual(1, ctx.exception.code)

    def test_cmd_adapt_all_dispatches_pending_projects(self):
        from skua.commands.adapt import _cmd_adapt_all

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            store = self._new_store(tmp / "cfg")

            p1_dir = tmp / "proj-a"
            p1_dir.mkdir()
            p2_dir = tmp / "proj-b"
            p2_dir.mkdir()

            store.save_resource(Project(name="proj-a", directory=str(p1_dir), agent="codex"))
            store.save_resource(Project(name="proj-b", directory=str(p2_dir), agent="codex"))

            ensure_adapt_workspace(p1_dir, "proj-a", "codex")
            ensure_adapt_workspace(p2_dir, "proj-b", "codex")
            (p1_dir / ".skua" / "image-request.yaml").write_text(
                yaml.dump(
                    {"schemaVersion": 1, "status": "ready", "packages": ["git"]},
                    default_flow_style=False,
                    sort_keys=False,
                )
            )

            args = argparse.Namespace(
                all=True,
                show_prompt=False,
                discover=False,
                clear=False,
                write_only=False,
                base_image="",
                from_image="",
                package=[],
                extra_command=[],
                build=False,
                apply_only=False,
                force=False,
            )

            with mock.patch("skua.commands.adapt.cmd_adapt") as mock_cmd:
                _cmd_adapt_all(store, args)

            self.assertEqual(1, mock_cmd.call_count)
            called_args = mock_cmd.call_args.args[0]
            self.assertEqual("proj-a", called_args.name)
            self.assertFalse(called_args.all)

    def test_agent_adapt_command_simple_template_avoids_shell(self):
        from skua.commands.adapt import _agent_adapt_command

        agent = AgentConfig(
            name="claude",
            runtime=AgentRuntimeSpec(
                command="claude",
                adapt_command='claude -p "{prompt}"',
            ),
        )

        cmd = _agent_adapt_command(agent, "proj")
        self.assertNotEqual(cmd[:2], ["bash", "-lc"])
        self.assertEqual(cmd[0], "claude")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("status: ready", cmd[-1])
        self.assertIn(".skua/image-request.yaml", cmd[-1])

    def test_agent_adapt_command_template_without_quotes_keeps_prompt_single_arg(self):
        from skua.commands.adapt import _agent_adapt_command, _agent_prompt

        agent = AgentConfig(
            name="claude",
            runtime=AgentRuntimeSpec(
                command="claude",
                adapt_command="claude -p {prompt}",
            ),
        )

        prompt = _agent_prompt("proj", "claude")
        cmd = _agent_adapt_command(agent, "proj")
        prompt_index = cmd.index("-p") + 1
        self.assertEqual(prompt, cmd[prompt_index])
        self.assertIn("status: ready", cmd[prompt_index])

    def test_agent_adapt_command_default_claude_includes_permission_flag(self):
        from skua.commands.adapt import _agent_adapt_command

        agent = AgentConfig(
            name="claude",
            runtime=AgentRuntimeSpec(command="claude"),
        )

        cmd = _agent_adapt_command(agent, "proj")
        self.assertEqual(cmd[0], "claude")
        self.assertIn("--dangerously-skip-permissions", cmd)
        self.assertIn("-p", cmd)

    def test_agent_adapt_command_shell_template_uses_bash(self):
        from skua.commands.adapt import _agent_adapt_command

        agent = AgentConfig(
            name="mock-agent",
            runtime=AgentRuntimeSpec(
                command="bash",
                adapt_command=(
                    "cat > .skua/image-request.yaml <<'EOF'\n"
                    "schemaVersion: 1\n"
                    "status: ready\n"
                    "EOF"
                ),
            ),
        )

        cmd = _agent_adapt_command(agent, "proj")
        self.assertEqual(cmd[:2], ["bash", "-lc"])
        self.assertIn("cat > .skua/image-request.yaml", cmd[2])

    def test_cmd_adapt_asks_agent_to_create_smoke_test_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump({"schemaVersion": 1, "status": "ready", "packages": ["git"]},
                          f, default_flow_style=False, sort_keys=False)

            session_calls = []
            def fake_session(store, project, env, sec, agent, build_error="", smoke_error="",
                             prompt_override="", warn_on_failure=False):
                session_calls.append({"prompt_override": prompt_override, "warn_on_failure": warn_on_failure})

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session", side_effect=fake_session),
                mock.patch("skua.commands.adapt._build_project_image", return_value=""),
                mock.patch("skua.commands.adapt._run_smoke_test", return_value=""),
            ):
                cmd_adapt(self._adapt_args("proj", build=True))

            # Should have called the session once for smoke test creation (build succeeded, no smoke test existed)
            self.assertEqual(1, len(session_calls))
            self.assertTrue(session_calls[0]["warn_on_failure"])
            self.assertIn("smoke-test.sh", session_calls[0]["prompt_override"])

    def test_cmd_adapt_smoke_test_passes_exits_loop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump({"schemaVersion": 1, "status": "ready", "packages": ["git"]},
                          f, default_flow_style=False, sort_keys=False)
            # Pre-create smoke test so creation session is skipped
            smoke_script = smoke_test_path(project_dir)
            smoke_script.write_text("#!/bin/bash\nexit 0\n")

            session_calls = []
            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session", side_effect=session_calls.append),
                mock.patch("skua.commands.adapt._build_project_image", return_value=""),
                mock.patch("skua.commands.adapt._run_smoke_test", return_value=""),
            ):
                cmd_adapt(self._adapt_args("proj", build=True))

            # No retries needed — build and smoke test both passed
            self.assertEqual(0, len(session_calls))

    def test_cmd_adapt_smoke_test_failure_triggers_agent_revision(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_dir = Path(tmpdir) / "repo"
            project_dir.mkdir()
            store = self._new_store(Path(tmpdir) / "cfg")
            store.save_resource(Project(name="proj", directory=str(project_dir), agent="codex"))

            _, request_path = ensure_adapt_workspace(project_dir, "proj", "codex")
            with open(request_path, "w") as f:
                yaml.dump({"schemaVersion": 1, "status": "ready", "packages": ["git"]},
                          f, default_flow_style=False, sort_keys=False)
            smoke_script = smoke_test_path(project_dir)
            smoke_script.write_text("#!/bin/bash\nexit 1\n")

            # Smoke test fails first, then passes after agent revision
            smoke_calls = [0]
            def fake_smoke(store, project, project_dir, smoke_script):
                smoke_calls[0] += 1
                if smoke_calls[0] == 1:
                    return "python3: command not found"
                return ""

            session_calls = []
            def fake_session(store, project, env, sec, agent, build_error="", smoke_error="",
                             prompt_override="", warn_on_failure=False):
                session_calls.append({"smoke_error": smoke_error})
                with open(request_path, "w") as f:
                    yaml.dump({"schemaVersion": 1, "status": "ready", "packages": ["git", "python3"]},
                              f, default_flow_style=False, sort_keys=False)

            with (
                mock.patch("skua.commands.adapt.ConfigStore", return_value=store),
                mock.patch("skua.commands.adapt._run_agent_adapt_session", side_effect=fake_session),
                mock.patch("skua.commands.adapt._build_project_image", return_value=""),
                mock.patch("skua.commands.adapt._run_smoke_test", side_effect=fake_smoke),
            ):
                cmd_adapt(self._adapt_args("proj", build=True))

            self.assertEqual(1, len(session_calls))
            self.assertIn("python3: command not found", session_calls[0]["smoke_error"])
            self.assertEqual(2, smoke_calls[0])
            updated = store.load_project("proj")
            self.assertIn("python3", updated.image.extra_packages)

    def test_agent_prompt_includes_smoke_error_context(self):
        from skua.commands.adapt import _agent_prompt

        prompt = _agent_prompt("proj", "claude", smoke_error="python3: command not found")
        self.assertIn("SMOKE TEST FAILED", prompt)
        self.assertIn("python3: command not found", prompt)

    def test_agent_smoke_test_creation_prompt_describes_script(self):
        from skua.commands.adapt import _agent_smoke_test_creation_prompt

        prompt = _agent_smoke_test_creation_prompt("myproject")
        self.assertIn("smoke-test.sh", prompt)
        self.assertIn("exit 0", prompt)
        self.assertIn("myproject", prompt)

    def test_smoke_test_path_returns_skua_subpath(self):
        p = smoke_test_path(Path("/some/project"))
        self.assertEqual(p, Path("/some/project/.skua/smoke-test.sh"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
