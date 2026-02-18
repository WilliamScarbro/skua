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

Build Docker images required by currently configured projects. For default projects, Skua tags images as `<imageName>-<agent>` (for example `skua-base-codex`). Projects with image customizations use project-scoped tags (`...-<project>-vN`).
If an image already exists, Skua compares its saved build-context hash (generated Dockerfile + entrypoint/default config inputs) and rebuilds automatically when drift is detected.

```bash
skua build
```

The image name, base image, and extra packages are configured in global config:

```bash
skua config --tool-dir /path/to/skua
```

### `skua adapt <name>`

Start the project container, run the configured agent to update `.skua/image-request.yaml`, apply the request into project config, and build the updated project image.

```bash
skua adapt myapp                    # run agent + apply request + build adapted image
skua adapt myapp --build            # apply + build now
skua adapt myapp --apply-only       # skip agent run; apply existing request file
skua adapt myapp --from-image ghcr.io/acme/app:dev
skua adapt myapp --base-image debian:bookworm-slim --package libpq-dev
skua adapt myapp --command "npm ci" --command "npm run build"
skua adapt myapp --clear            # remove project-specific image customization
```

If the agent is not logged in, `skua adapt` exits with an error and asks you to authenticate via `skua run <name>`.

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

### `skua run <name>`

Start a container for a project. Validates configuration before launching. Skua starts the container in detached mode and attaches to a persistent in-container `tmux` session by default.

For bind persistence, Skua auto-seeds missing agent auth files from host home into the project's persisted auth directory on first run (for example Codex `~/.codex/auth.json`).
Use `skua adapt <name>` to have the agent generate/apply image updates in one command.

```bash
skua run myapp
```

Detach while keeping container/session alive with `Ctrl-b`, then `d`. Re-run `skua run myapp` to reattach.

### `skua list`

List all configured projects and their running status.

```bash
skua list
skua list -a        # include agent/credential columns
skua list -s        # include security/network columns
skua list -a -s     # full view
```

Default columns: NAME, SOURCE, STATUS.

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
