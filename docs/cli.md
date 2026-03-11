<!-- SPDX-License-Identifier: BUSL-1.1 -->
# CLI Reference

## Global Options

```
skua --version    Show version
skua --help       Show help
```

## Commands

### `skua init`

First-time setup wizard. Creates `~/.config/skua/` directory structure, collects git identity and SSH key, installs shipped presets, saves global config.

```bash
skua init           # interactive wizard
skua init --force   # re-initialize (overwrites global config)
```

### `skua build`

Build the Docker image required by a specific project. For default projects, Skua tags images as `<imageName>-<agent>` (for example `skua-base-codex`). Projects with image customizations use project-scoped tags (`...-<project>-vN`).
If an image already exists, Skua compares its saved build-context hash (generated Dockerfile + entrypoint/default config inputs) and rebuilds automatically when drift is detected.

```bash
skua build myapp
```

The image name, base image, and extra packages are configured in global config:

```bash
skua config --tool-dir /path/to/skua
```

### `skua adapt <name>`

Apply latent `.skua/image-request.yaml` updates into project image config.
Use `--discover` to run the configured agent first and generate/update that wishlist automatically.
Skua also creates `AGENTS.md` and `CLAUDE.md` in the project root to guide interactive agent sessions.

```bash
skua adapt myapp                    # apply latent wishlist from .skua/image-request.yaml
skua adapt --all                    # apply latent wishlist updates for all pending projects
skua adapt myapp --show-prompt      # print resolved agent prompt/command and exit
skua adapt myapp --discover         # run agent discovery + apply + build adapted image
skua adapt myapp --build            # apply + build now
skua adapt myapp --apply-only       # skip agent run; apply existing request file
skua adapt myapp --from-image ghcr.io/acme/app:dev
skua adapt myapp --base-image debian:bookworm-slim --package libpq-dev
skua adapt myapp --command "npm ci" --command "npm run build"
skua adapt myapp --clear            # remove project-specific image customization
```

If the agent is not logged in, `skua adapt --discover` exits with an error and asks you to authenticate via `skua run <name>`.

### `skua add <name>`

Add a project configuration.

```bash
skua add myapp --dir ~/projects/myapp
skua add myapp --dir ~/projects/myapp --quick           # skip prompts
skua add myapp --dir ~/projects/myapp --no-prompt        # skip prompts for missing values
skua add myapp --dir ~/projects/myapp \
    --ssh-key ~/.ssh/id_ed25519 \
    --env local-docker-gvisor \
    --security standard \
    --agent codex
```

| Option | Description |
|--------|-------------|
| `--dir` | Project directory path (bind-mounted into container) |
| `--ssh-key` | SSH private key path for git operations |
| `--env` | Environment resource name (default: from global config) |
| `--security` | Security profile name (default: from global config) |
| `--agent` | Agent config name (default: from global config) |
| `--quick` | Use all defaults, skip all prompts |
| `--no-prompt` | Skip prompts for missing values |

### `skua remove <name>`

Remove a project configuration. Optionally removes persisted agent data.

```bash
skua remove myapp
```

For remote projects (`spec.host` set), `skua remove` can also remove remote Docker resources for that project:
- container (`skua-<name>`)
- auth volume (`skua-<name>-<agent>`)
- repo volume (`skua-<name>-repo`, when repo-backed)
- project image tag used by the project

### `skua run <name>`

Start a container for a project. Validates configuration before launching. Skua starts the container in detached mode and attaches to a persistent in-container `tmux` session by default.

For bind persistence, Skua auto-seeds missing agent auth files from host home into the project's persisted auth directory on first run (for example Codex `~/.codex/auth.json`).
Interactive Bash history is persisted per project at `<project>/.skua/.bash_history`.
Use `skua adapt <name>` to have the agent generate/apply image updates in one command.

```bash
skua run myapp
skua run myapp --no-attach
```

Remote project behavior (`spec.host` set):
- Skua first tries `DOCKER_HOST=ssh://<host>` transport.
- If that fails (for example Snap Docker CLI cannot exec `ssh`), Skua offers:
  1. install standalone non-Snap Docker CLI now
  2. continue with SSH fallback transport (`ssh <host> docker ...`)
  3. cancel
- Option 1 runs the bundled installer script (`skua/scripts/install_docker_cli.sh`) and retries.
- Project SSH key and known_hosts are injected into remote clone/run paths.
- Agent auth files are seeded into the remote auth volume on startup.

Detach while keeping container/session alive with `Ctrl-b`, then `d`. Re-run `skua run myapp` to reattach.

`--no-attach` is useful for automation flows that need the container running but will invoke the agent later with `skua task prompt` or similar commands.

### `skua task`

Plan and launch multi-agent work from a directory of markdown briefs. This is intended for workflows like `/home/dev/image_refactor_plan`, where each numbered file represents one workstream.

```bash
skua task plan /home/dev/image_refactor_plan
skua task plan /home/dev/image_refactor_plan --format yaml --write /tmp/refactor-plan.yaml
skua task prompt repo-agent --prompt-file /home/dev/image_refactor_plan/01-repo-worktree-agent.md --ensure-running
skua task dispatch /home/dev/image_refactor_plan --project-prefix image-refactor-         # preview mapping only
skua task dispatch /home/dev/image_refactor_plan --project-prefix image-refactor- --execute --ensure-running --background
```

Subcommands:
- `plan`: reads numbered `*.md` briefs, uses `README.md` suggested execution order when present, and prints a normalized task summary.
- `prompt`: runs the configured agent's non-interactive prompt command inside an existing project container. Use `--ensure-running` to start the container in detached mode first.
- `dispatch`: maps each task brief to a project, either via repeated `--project` values or a generated `--project-prefix`, then optionally launches all prompts.

Notes:
- `prompt` prefers the agent resource's `runtime.prompt_command`, then falls back to `runtime.adapt_command`.
- `dispatch --background` runs each task detached in the container and prints a per-task log path under `/tmp/`.
- For Codex and Claude presets, Skua ships prompt-command defaults suitable for non-interactive execution.

## Python Library

The same task workflow is available as an importable library, so orchestration code does not need to shell out through the CLI.

```python
from skua import (
    dispatch_task_plan,
    load_task_brief,
    make_task_plan,
    run_task_prompt,
)

schema = load_task_brief("/home/dev/image_refactor_plan/03-schema-migration-agent.md")
repo = load_task_brief(
    "/home/dev/image_refactor_plan/01-repo-worktree-agent.md",
    depends_on=[schema.brief_file],
)
cli = load_task_brief(
    "/home/dev/image_refactor_plan/04-cli-ux-agent.md",
    depends_on=[repo.brief_file],
)

plan = make_task_plan(
    tasks=[schema, repo, cli],
    suggested_order=[schema.brief_file, repo.brief_file, cli.brief_file],
)

# Preview project mapping
mappings, _ = dispatch_task_plan(plan, project_prefix="image-refactor-")

# Launch the full batch
mappings, executions = dispatch_task_plan(
    plan,
    project_prefix="image-refactor-",
    execute=True,
    ensure_running=True,
    background=True,
)

# Or run one prompt directly
result = run_task_prompt(
    project_name="image-refactor-schema-migration-agent",
    prompt="Read the assigned brief and implement only that workstream.",
    ensure_running=True,
    background=True,
)
```

Primary imports:
- `load_task_brief(path, depends_on=...)` -> `TaskBrief`
- `make_task_plan(tasks, ...)` -> `TaskPlan`
- `dispatch_task_plan(plan, ...)` -> `(mappings, executions)`
- `run_task_prompt(project_name, prompt, ...)` -> `PromptExecution`
- `resolve_task_projects(plan, ...)` -> project mapping without execution

`load_task_plan(path)` still exists for the CLI-oriented "directory of briefs" workflow, but the recommended Python integration is to encode the plan structure in Python and load each brief explicitly.

### `skua list`

List all configured projects and their running status.

```bash
skua list
skua list -a        # include agent/credential columns
skua list -s        # include security/network columns
skua list -a -s     # full view
```

Default columns: NAME, ACTIVITY, STATUS, SOURCE.

### `skua dashboard`

Start a live, interactive dashboard with a continuously refreshing project table.
It uses the same filtering/column flags as `skua list`.
The dashboard UI is powered by Textual.

```bash
skua dashboard
skua dashboard -a -s -g -i
skua dashboard --local
```

Keybindings:
- `h`: toggle help
- `Tab`: switch focus between projects and jobs
- `Up` / `Down`: move selection in focused pane
- `Enter`: run selected project
- `b`: queue background build job for selected project
- `s`: queue background stop job for selected project
- `a`: queue background adapt job for selected project
- `d`: remove selected project (projects view) or remove selected job entry (jobs view)
- `r`: restart selected project
- `n`: create a new project (interactive add flow)
- `o`: toggle selected job output viewer
- `x`: cancel selected running job
- `c`: clear completed jobs
- `y`: export selected job output (choose save-to-file, clipboard, or both)
- `q`: quit

The dashboard auto-refreshes every 2 seconds. Background job history and logs are persisted in `~/.config/skua/jobs/`.
The bottom command bar is context-aware and changes based on whether focus is in projects, jobs, or job output view.
Pressing `n` opens an in-dashboard wizard in the status bar (no terminal suspend): type text fields, use `←/→` or `↑/↓` for selectors, `Enter` to advance, and `Esc` to cancel.
Background jobs now run in a PTY and can enter a `waiting_input` state when they prompt. Open the job output view and type a reply in the status bar, then press `Enter` to send input back to the running job.

### `skua clean [<name>]`

Remove saved agent credentials for a project (or all projects).

```bash
skua clean myapp    # one project
skua clean          # all projects (with confirmation)
```

### `skua purge`

Remove all local skua state: project config, skua containers, skua volumes, and skua images.

```bash
skua purge          # interactive confirmation
skua purge --yes    # no prompts
```

### `skua config`

Show or edit global configuration.

```bash
# View current config
skua config

# Set values
skua config --git-name "Your Name"
skua config --git-email "you@example.com"
skua config --ssh-key ~/.ssh/id_ed25519
skua config --tool-dir /path/to/skua
skua config --default-env local-docker-gvisor
skua config --default-security standard
skua config --default-agent codex
```

### `skua validate <name>`

Validate project configuration consistency. Checks:
1. Environment internal consistency (e.g., managed mode requires compose/k8s)
2. Security profile internal consistency (e.g., verified installs require sudo:false)
3. Security profile requirements vs environment capabilities
4. Agent compatibility with security profile

```bash
skua validate myapp
```

### `skua describe <name>`

Show the fully resolved configuration for a project, including all referenced resources dumped as YAML.

```bash
skua describe myapp
```

## Configuration Files

All configuration is stored in `~/.config/skua/`:

```
~/.config/skua/
├── global.yaml              # git identity, defaults, tool directory
├── environments/            # Environment resources
├── security/                # SecurityProfile resources
├── agents/                  # AgentConfig resources
├── projects/                # Project resources
├── claude-data/             # persisted default/legacy auth data (bind mode)
└── agent-data/              # persisted per-agent auth data (bind mode)
```

Each resource is a standalone YAML file with `apiVersion`, `kind`, `metadata`, and `spec` fields. Edit them directly or use CLI commands.
