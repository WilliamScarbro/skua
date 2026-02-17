# SPDX-License-Identifier: BUSL-1.1
"""skua purge — remove all local skua state."""

import shutil
import subprocess

from skua.config import ConfigStore
from skua.utils import confirm


def _repo_from_ref(image_ref: str) -> str:
    """Extract repository from image ref while handling registry ports."""
    ref = (image_ref or "").strip()
    if not ref or ref.startswith("<none>"):
        return ""

    slash_idx = ref.rfind("/")
    colon_idx = ref.rfind(":")
    if colon_idx > slash_idx:
        return ref[:colon_idx]
    return ref


def _repo_from_image_name(image_name: str) -> str:
    """Extract repository from configured image name while handling tags."""
    name = (image_name or "skua-base").strip() or "skua-base"
    slash_idx = name.rfind("/")
    colon_idx = name.rfind(":")
    if colon_idx > slash_idx:
        return name[:colon_idx]
    return name


def _select_images_for_purge(image_refs: list, image_name_base: str) -> list:
    """Select image refs that belong to skua naming conventions."""
    targets = {_repo_from_image_name(image_name_base), "skua-base"}
    prefixes = tuple(f"{repo}-" for repo in targets if repo)

    selected = []
    seen = set()
    for ref in image_refs:
        repo = _repo_from_ref(ref)
        if not repo:
            continue
        if repo in targets or repo.startswith(prefixes):
            if ref not in seen:
                seen.add(ref)
                selected.append(ref)
    return selected


def _docker_lines(cmd: list) -> list:
    """Run a docker list command and return non-empty output lines."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return []
    if result.returncode != 0:
        return []
    return [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]


def _run_remove(cmd: list, label: str):
    """Run a removal command and print warnings if it fails."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        print(f"Warning: docker not found; skipping {label}.")
        return
    if result.returncode != 0:
        err = result.stderr.strip() or result.stdout.strip() or "unknown error"
        print(f"Warning: failed to remove {label}: {err}")


def cmd_purge(args):
    """Remove all skua-managed local state (with confirmation)."""
    store = ConfigStore()
    g = store.load_global() if store.global_file.exists() else {}
    image_name_base = g.get("imageName", "skua-base")

    containers = _docker_lines(["docker", "ps", "-aq", "--filter", "name=^skua-"])
    volumes = _docker_lines(["docker", "volume", "ls", "-q", "--filter", "name=^skua-"])
    images_all = _docker_lines(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"])
    images = _select_images_for_purge(images_all, image_name_base)
    config_exists = store.config_dir.exists()
    projects = store.list_resources("Project") if config_exists else []

    print("About to purge skua local state:")
    print(f"  Projects:   {len(projects)}")
    print(f"  Containers: {len(containers)}")
    print(f"  Volumes:    {len(volumes)}")
    print(f"  Images:     {len(images)}")
    print(f"  Config dir: {store.config_dir if config_exists else '(none)'}")

    if not any([containers, volumes, images, config_exists]):
        print("Nothing to purge.")
        return

    if not getattr(args, "yes", False):
        if not confirm("Proceed with purge? This cannot be undone.", default=False):
            print("Purge cancelled.")
            return
        token = input("Type 'purge' to confirm: ").strip().lower()
        if token != "purge":
            print("Purge cancelled.")
            return

    if containers:
        _run_remove(["docker", "rm", "-f", *containers], "containers")
    if volumes:
        _run_remove(["docker", "volume", "rm", *volumes], "volumes")
    if images:
        _run_remove(["docker", "image", "rm", "-f", *images], "images")

    if config_exists:
        shutil.rmtree(store.config_dir, ignore_errors=True)
        print(f"Removed config directory: {store.config_dir}")

    print("Purge complete.")
