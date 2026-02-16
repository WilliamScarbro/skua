# cdev — Claude Dev Environment Manager

Dockerized Claude Code environment with multi-project support, persistent authentication, and host-mounted projects.

## Quick Start

```bash
git clone https://github.com/WilliamScarbro/Claude_in_Docker.git
cd Claude_in_Docker
./install.sh
```

The installer will:
1. Check prerequisites (Docker, Python 3, git)
2. Collect your git name and email
3. Optionally configure an SSH key for git operations
4. Install the `cdev` CLI to your PATH
5. Build the base Docker image

Then add a project and start working:

```bash
cdev add myapp --dir ~/projects/myapp
cdev run myapp

# On first run inside the container:
claude login
```

Subsequent `cdev run` invocations reuse saved credentials — no re-login needed.

### Prerequisites

- Docker (daemon must be running)
- Python 3
- git

### Manual Installation

If you prefer not to use the install script:

```bash
ln -s /path/to/this/repo/cdev ~/.local/bin/cdev
cdev config --git-name "Your Name" --git-email "you@example.com"
cdev build
```

## Commands

| Command | Purpose |
|---------|---------|
| `cdev build` | Build the base Docker image (`cdev-base`) |
| `cdev add <name>` | Add a project configuration |
| `cdev remove <name>` | Remove a project configuration |
| `cdev run <name>` | Start a container for a project (or attach if already running) |
| `cdev list` | List all projects and their running status |
| `cdev clean [<name>]` | Remove saved Claude credentials for a project (or all) |
| `cdev config` | Show or edit global configuration |

## Adding Projects

```bash
# Interactive (prompts for missing values)
cdev add myapp --dir ~/projects/myapp

# Fully specified (no prompts)
cdev add myapp \
  --dir ~/projects/myapp \
  --ssh-key ~/.ssh/id_ed25519 \
  --network host \
  --persist bind \
  --no-prompt
```

### Options

- **`--dir`** — Host directory bind-mounted to `/home/dev/project` (read-write)
- **`--ssh-key`** — SSH private key mounted read-only for git operations
- **`--network`** — `bridge` (default) or `host` (shares host network stack, may help with OAuth)
- **`--persist`** — `bind` (default, stored at `~/.config/cdev/claude-data/<name>/`) or `volume` (Docker named volume)

## Multi-Project Workflow

Each project gets its own container (`cdev-<name>`) and its own Claude credentials, so you can work on multiple projects simultaneously:

```bash
cdev add frontend --dir ~/projects/frontend
cdev add backend --dir ~/projects/backend --ssh-key ~/.ssh/id_ed25519

# Run in separate terminals
cdev run frontend
cdev run backend

# See what's running
cdev list
```

## Configuration

Global config is stored at `~/.config/cdev/config.json` and serves as the default for all projects.

```bash
# Set git identity
cdev config --git-name "Your Name" --git-email "you@example.com"

# Set global defaults (inherited by all projects)
cdev config --ssh-key ~/.ssh/id_ed25519
cdev config --network host
cdev config --persist bind

# Set tool directory (auto-detected by default)
cdev config --tool-dir /path/to/this/repo

# View current config
cdev config
```

### Global Defaults

The following settings can be configured globally and are inherited by all projects unless overridden:

| Setting | Flag | Default | Description |
|---------|------|---------|-------------|
| SSH key | `--ssh-key` | (none) | Default SSH private key for git operations |
| Network | `--network` | `bridge` | Docker network mode (`bridge` or `host`) |
| Persist | `--persist` | `bind` | Claude credential storage (`bind` or `volume`) |

When adding a project, only explicitly provided values are stored. Missing values fall through the resolution chain:

**project local** → **global config** → **hardcoded default**

The `add` command shows where each resolved value comes from:

```
Project 'myapp' added.
  Directory:   ~/projects/myapp
  SSH key:     ~/.ssh/id_ed25519 (global)
  Network:     bridge (local)
  Persist:     bind (default)
```

## Auth Persistence

Claude Code credentials are stored per-project:

- **Bind mode** (default): `~/.config/cdev/claude-data/<project>/`
- **Volume mode**: Docker named volume `cdev-<project>-claude`

To wipe credentials and force a fresh login:

```bash
cdev clean myapp      # one project
cdev clean            # all projects
```

## Container Commands

| Command | Description |
|---------|-------------|
| `claude` | Start Claude Code |
| `claude-dsp` | Start Claude Code with `--dangerously-skip-permissions` |

## Architecture

- **Base image:** `debian:bookworm-slim` — one shared image for all projects
- **Claude install:** Native installer (`~/.local/bin/claude`)
- **User:** `dev` (UID/GID matched to host to avoid permission issues)
- **No secrets in the image** — credentials live only in runtime mounts
- **Project isolation** — each project has its own container name, credentials, and mount
