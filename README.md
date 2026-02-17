<!-- SPDX-License-Identifier: BUSL-1.1 -->
<p align="center">
  <img src="docs/logo.png" alt="Skua logo" width="300">
</p>

# Skua — Dockerized Coding Agent Manager

Run coding agents (Claude, Codex, and more) in isolated Docker containers with configurable security profiles, multi-project support, and persistent authentication.

## Getting Started

### 1. Install Skua
```bash
git clone https://github.com/WilliamScarbro/skua.git
cd skua
./install.sh
```

### 2. Add a project
Use a local directory:

```bash
skua add myapp --dir ~/projects/myapp
```

Or use a GitHub repo URL:

```bash
skua add myapp --repo git@github.com:your-org/myapp.git
```

### 3. Run the project container

```bash
skua run myapp
```

### 4. Start Claude in the container
Inside the container shell:

```bash
claude
```

First run may require login:

```bash
claude login
```

Credentials are persisted, so later runs usually do not require login again.

### 5. Detach from the running container
Keep the container running and detach your terminal:

- Press `Ctrl-p`, then `Ctrl-q`

### 6. Reattach later
Run the same command again and accept the attach prompt:

```bash
skua run myapp
```

### 7. Stop the container when done
If attached, run `exit`.  
If detached, stop it from the host:

```bash
docker stop skua-myapp
```

### Prerequisites

- Docker (daemon running)
- Python 3 + PyYAML
- git

### Alternative: .deb Package

```bash
sudo dpkg -i skua_<version>_all.deb
skua init
skua build
```

### Manual Setup

```bash
pip install pyyaml
ln -s /path/to/skua/bin/skua ~/.local/bin/skua
skua init
skua build
```

## Useful Docs

- **[Quick Start](docs/quickstart.md)**: Installation and first project flow
- **[CLI Reference](docs/cli.md)**: All commands and options
- **[Configuration Model](docs/configuration.md)**: YAML resources, environments, validation
- **[Security Guide](docs/security.md)**: Security profiles, trust model, isolation

## Common Commands

| Command | Purpose |
|---------|---------|
| `skua init` | First-time setup wizard |
| `skua build` | Build images required by configured projects |
| `skua add <name>` | Add a project (`--dir` or `--repo`) |
| `skua run <name>` | Start a container (or attach if already running) |
| `skua list` | List projects and running status |
| `skua config` | Show or edit global configuration |
| `skua validate <name>` | Validate project configuration |
| `skua describe <name>` | Show resolved configuration as YAML |
| `skua clean [name]` | Remove saved credentials |
| `skua remove <name>` | Remove a project |
| `skua purge` | Remove all local skua state (config + Docker artifacts) |

For advanced security/environment setup, go to:
- `docs/security.md`
- `docs/configuration.md`

## License

Business Source License 1.1 — see [LICENSE](LICENSE).
