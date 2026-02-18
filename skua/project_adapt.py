# SPDX-License-Identifier: BUSL-1.1
"""Project image-adapt helpers.

This module manages per-project adapt guidance and image request templates.
"""

from pathlib import Path

import yaml


ADAPT_DIRNAME = ".skua"
ADAPT_GUIDE_NAME = "ADAPT.md"
IMAGE_REQUEST_NAME = "image-request.yaml"


def adapt_dir(project_dir: Path) -> Path:
    """Return the .skua adapt directory path for a project."""
    return project_dir / ADAPT_DIRNAME


def adapt_guide_path(project_dir: Path) -> Path:
    """Return the adapt guide path for a project."""
    return adapt_dir(project_dir) / ADAPT_GUIDE_NAME


def image_request_path(project_dir: Path) -> Path:
    """Return the image request template path for a project."""
    return adapt_dir(project_dir) / IMAGE_REQUEST_NAME


def ensure_adapt_workspace(project_dir: Path, project_name: str, agent_name: str) -> tuple[Path, Path]:
    """Create per-project adapt files if missing and return (guide, request) paths."""
    d = adapt_dir(project_dir)
    d.mkdir(parents=True, exist_ok=True)

    guide = adapt_guide_path(project_dir)
    if not guide.exists():
        guide.write_text(_adapt_guide_text(project_name=project_name, agent_name=agent_name))

    request = image_request_path(project_dir)
    if not request.exists():
        request.write_text(_image_request_template_text())

    _ensure_git_exclude(
        project_dir,
        [
            f"{ADAPT_DIRNAME}/{IMAGE_REQUEST_NAME}",
            f"{ADAPT_DIRNAME}/{ADAPT_GUIDE_NAME}",
        ],
    )
    return guide, request


def load_image_request(path: Path) -> dict:
    """Load and normalize an image-request YAML file."""
    raw = {}
    if path.exists():
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raw = {}
    return normalize_image_request(raw)


def normalize_image_request(data: dict) -> dict:
    """Normalize request keys and values to a stable internal shape."""
    def _pick(*keys, default=""):
        for k in keys:
            v = data.get(k)
            if v is not None:
                return str(v).strip()
        return default

    def _list(*keys):
        vals = []
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                vals.extend(v)
        out = []
        seen = set()
        for v in vals:
            s = str(v).strip()
            if s and s not in seen:
                seen.add(s)
                out.append(s)
        return out

    return {
        "schemaVersion": int(data.get("schemaVersion") or 1),
        "status": _pick("status", default="draft").lower() or "draft",
        "summary": _pick("summary"),
        "baseImage": _pick("baseImage", "base_image"),
        "fromImage": _pick("fromImage", "from_image"),
        "packages": _list("packages", "extraPackages", "extra_packages"),
        "commands": _list("commands", "extraCommands", "extra_commands"),
    }


def request_has_updates(request: dict) -> bool:
    """True when the request contains actual image customization data."""
    req = normalize_image_request(request or {})
    return bool(
        req.get("baseImage")
        or req.get("fromImage")
        or req.get("packages")
        or req.get("commands")
    )


def apply_image_request_to_project(project, request: dict) -> bool:
    """Apply normalized request fields to project.image; bump version if changed."""
    req = normalize_image_request(request or {})

    current = (
        str(getattr(project.image, "base_image", "") or "").strip(),
        str(getattr(project.image, "from_image", "") or "").strip(),
        list(getattr(project.image, "extra_packages", []) or []),
        list(getattr(project.image, "extra_commands", []) or []),
    )
    desired = (
        req["baseImage"],
        req["fromImage"],
        req["packages"],
        req["commands"],
    )
    if current == desired:
        return False

    project.image.base_image = req["baseImage"]
    project.image.from_image = req["fromImage"]
    project.image.extra_packages = req["packages"]
    project.image.extra_commands = req["commands"]
    old_version = int(getattr(project.image, "version", 0) or 0)
    project.image.version = old_version + 1
    return True


def write_applied_image_request(path: Path, request: dict, version: int):
    """Persist the request file with applied status and applied version."""
    req = normalize_image_request(request or {})
    req["status"] = "applied"
    req["appliedVersion"] = int(version or 0)
    with open(path, "w") as f:
        yaml.dump(req, f, default_flow_style=False, sort_keys=False)


def _adapt_guide_text(project_name: str, agent_name: str) -> str:
    return f"""# SPDX-License-Identifier: BUSL-1.1
# Skua Image Adapt ({project_name})

Use this workflow to let `{agent_name}` suggest container image changes without writing a Dockerfile.

1. Run the project container: `skua run {project_name}`
2. Ask `{agent_name}` to inspect the repo and update:
   - `.skua/{IMAGE_REQUEST_NAME}`
3. On the host, apply that request:
   - `skua adapt {project_name}`
4. Start again with updated image config:
   - `skua run {project_name}`

Request rules:
- Prefer `packages` for apt package names.
- Use `baseImage` to switch to a different base image.
- Use `fromImage` to adapt an existing working image as the parent image.
- Use `commands` for additional setup commands.
- Do not write a Dockerfile directly; only update `.skua/{IMAGE_REQUEST_NAME}`.
"""


def _image_request_template_text() -> str:
    return f"""# SPDX-License-Identifier: BUSL-1.1
# Skua image request template (filled by your agent and applied by `skua adapt`)
schemaVersion: 1
status: draft
summary: ""

# Option A: switch to a different base image for the generated skua Dockerfile.
baseImage: ""

# Option B: adapt an existing image (used as Dockerfile FROM image).
fromImage: ""

# Apt package names to install in generated skua Dockerfile.
packages: []

# Additional setup commands (RUN lines) for generated skua Dockerfile.
commands: []
"""


def _ensure_git_exclude(project_dir: Path, patterns: list):
    """Add adapt artifacts to .git/info/exclude when this is a git repo."""
    git_dir = project_dir / ".git"
    if not git_dir.is_dir():
        return
    exclude_path = git_dir / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if exclude_path.exists():
        existing = {ln.strip() for ln in exclude_path.read_text().splitlines() if ln.strip()}
    missing = [p for p in patterns if p not in existing]
    if not missing:
        return
    with open(exclude_path, "a") as f:
        if existing:
            f.write("\n")
        for pattern in missing:
            f.write(f"{pattern}\n")
