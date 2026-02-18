<!-- SPDX-License-Identifier: BUSL-1.1 -->
# Quick Start

## Prerequisites

- **Docker** (daemon must be running)
- **Python 3** with **PyYAML** (`pip install pyyaml`)
- **git**

## Install from Source

```bash
git clone https://github.com/WilliamScarbro/skua.git
cd skua
./install.sh
```

The installer will:
1. Check prerequisites
2. Symlink `skua` to your PATH
3. Run the init wizard (git identity, SSH key, preset installation)
4. Build images required by configured projects

## Install from .deb

```bash
sudo dpkg -i skua_<version>_all.deb
skua init
skua build
```

## First Project

```bash
# Add a project
skua add myapp --dir ~/projects/myapp --agent codex

# Optional: run automated project image adaptation
skua adapt myapp

# Launch the container
skua run myapp

# Inside the container:
codex login     # copy the URL into your host browser
codex           # start coding
```

Subsequent `skua run` invocations reuse saved credentials — no re-login needed.

## Quick Mode

Skip all prompts and use defaults:

```bash
skua add myapp --dir ~/projects/myapp --quick
skua run myapp
```

## Multiple Projects

Each project gets its own container and credentials:

```bash
skua add frontend --dir ~/projects/frontend
skua add backend  --dir ~/projects/backend --ssh-key ~/.ssh/id_ed25519

# Run in separate terminals
skua run frontend
skua run backend

# See what's running
skua list
```

## Choosing a Security Profile

Skua ships four security profiles:

| Profile | Description | Use When |
|---------|-------------|----------|
| `open` | No restrictions, agent has sudo and internet | Development, trusted code |
| `standard` | Advisory tracking, agent has sudo | Default for most projects |
| `hardened` | No sudo, proxy-mediated network, verified installs | Untrusted code, security audits |
| `airgapped` | No network, no installs | Maximum isolation |

```bash
# Use a specific security profile
skua add myapp --dir ~/projects/myapp --security standard

# Validate the configuration
skua validate myapp
```

See [security.md](security.md) for details on each profile.

## Choosing an Environment

Environments describe where containers run:

| Environment | Mode | Description |
|-------------|------|-------------|
| `local-docker` | unmanaged | Single container, simplest setup |
| `local-docker-gvisor` | unmanaged | Single container with gVisor kernel isolation |
| `local-compose` | managed | Multi-container with skua sidecar for trusted monitoring |

```bash
skua add myapp --dir ~/projects/myapp --env local-docker-gvisor
```

See [configuration.md](configuration.md) for the full configuration model.

## Next Steps

- [CLI Reference](cli.md) — all commands and options
- [Security Guide](security.md) — understanding security profiles and trust boundaries
- [Configuration](configuration.md) — YAML resource model and validation rules
