<p align="center">
  <img src="skua_with_lobster.png" alt="Skua logo" width="300">
</p>

# Skua — Dockerized Claude Code Manager

Dockerized Claude Code environment with multi-project support, persistent authentication, and host-mounted projects.

## Quick Start

```bash
git clone https://github.com/WilliamScarbro/skua.git
cd skua
./install.sh
```

The installer will:
1. Check prerequisites (Docker, Python 3, git)
2. Collect your git name and email
3. Optionally configure an SSH key for git operations
4. Install the `skua` CLI to your PATH
5. Build the base Docker image

Then add a project and start working:

```bash
skua add myapp --dir ~/projects/myapp
skua run myapp

# On first run inside the container:
claude login
```

Subsequent `skua run` invocations reuse saved credentials — no re-login needed.

### Prerequisites

- Docker (daemon must be running)
- Python 3
- git

### Manual Installation

If you prefer not to use the install script:

```bash
ln -s /path/to/this/repo/skua ~/.local/bin/skua
skua config --git-name "Your Name" --git-email "you@example.com"
skua build
```

## Commands

| Command | Purpose |
|---------|---------|
| `skua build` | Build the base Docker image (`skua-base`) |
| `skua add <name>` | Add a project configuration |
| `skua remove <name>` | Remove a project configuration |
| `skua run <name>` | Start a container for a project (or attach if already running) |
| `skua list` | List all projects and their running status |
| `skua clean [<name>]` | Remove saved Claude credentials for a project (or all) |
| `skua config` | Show or edit global configuration |

## Adding Projects

```bash
# Interactive (prompts for missing values)
skua add myapp --dir ~/projects/myapp

# Fully specified (no prompts)
skua add myapp \
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
- **`--persist`** — `bind` (default, stored at `~/.config/skua/claude-data/<name>/`) or `volume` (Docker named volume)

## Multi-Project Workflow

Each project gets its own container (`skua-<name>`) and its own Claude credentials, so you can work on multiple projects simultaneously:

```bash
skua add frontend --dir ~/projects/frontend
skua add backend --dir ~/projects/backend --ssh-key ~/.ssh/id_ed25519

# Run in separate terminals
skua run frontend
skua run backend

# See what's running
skua list
```

## Configuration

Global config is stored at `~/.config/skua/config.json` and serves as the default for all projects.

```bash
# Set git identity
skua config --git-name "Your Name" --git-email "you@example.com"

# Set global defaults (inherited by all projects)
skua config --ssh-key ~/.ssh/id_ed25519
skua config --network host
skua config --persist bind

# Set tool directory (auto-detected by default)
skua config --tool-dir /path/to/this/repo

# View current config
skua config
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

- **Bind mode** (default): `~/.config/skua/claude-data/<project>/`
- **Volume mode**: Docker named volume `skua-<project>-claude`

To wipe credentials and force a fresh login:

```bash
skua clean myapp      # one project
skua clean            # all projects
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
