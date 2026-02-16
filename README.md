# cdev — Claude Dev Environment Manager

Dockerized Claude Code environment with multi-project support, persistent authentication, and host-mounted projects.

## Quick Start

```bash
# 1. Set up git identity
cdev config --git-name "Your Name" --git-email "you@example.com"

# 2. Build the base image
cdev build

# 3. Add a project
cdev add myapp --dir ~/projects/myapp

# 4. Run it
cdev run myapp

# 5. On first run, log in inside the container
claude login
# Copy the URL into your host browser to complete OAuth
```

Subsequent `cdev run` invocations reuse saved credentials — no re-login needed.

## Installation

Symlink `cdev` into your PATH:

```bash
ln -s /path/to/this/repo/cdev ~/.local/bin/cdev
```

No dependencies beyond Python 3 (stdlib only).

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

Global config is stored at `~/.config/cdev/config.json`:

```bash
# Set git identity
cdev config --git-name "Your Name" --git-email "you@example.com"

# Set tool directory (auto-detected by default)
cdev config --tool-dir /path/to/this/repo

# View current config
cdev config
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
