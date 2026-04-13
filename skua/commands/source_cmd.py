# SPDX-License-Identifier: BUSL-1.1
"""skua source — manage project sources (local directories and git repositories)."""

import sys
from pathlib import Path

from skua.config import ConfigStore
from skua.config.resources import ProjectSourceSpec, normalize_project_ssh, ProjectSshSpec
from skua.docker import _project_mount_path, _source_mount_path


def explicit_sources(project) -> list:
    """Return the project's explicit sources, materializing the implicit single source if needed."""
    sources = list(getattr(project, "sources", []) or [])
    if sources:
        return sources
    # Materialize the implicit single source from flat fields
    return [ProjectSourceSpec(
        project=project.name,
        name=project.name,
        directory=project.directory or "",
        repo=project.repo or "",
        host=getattr(project, "host", "") or "",
        ssh_private_key=getattr(getattr(project, "ssh", None), "private_key", "") or "",
        mount_path=_project_mount_path(project),
        primary=True,
    )]


def sync_project_primary(project, sources: list) -> None:
    """Keep project.directory/repo/host in sync with the primary source."""
    if not sources:
        return
    primary = next((s for s in sources if getattr(s, "primary", False)), None)
    if primary is None:
        sources[0].primary = True
        primary = sources[0]
    project.directory = getattr(primary, "directory", "") or ""
    project.repo = getattr(primary, "repo", "") or ""
    project.host = getattr(primary, "host", "") or ""


def _resolve_source(sources: list, ref: str):
    """Find a source by 1-based index or name. Returns (index, source) or (None, None)."""
    if ref.isdigit():
        idx = int(ref) - 1
        if 0 <= idx < len(sources):
            return idx, sources[idx]
    for idx, src in enumerate(sources):
        label = getattr(src, "name", "") or getattr(src, "project", "") or ""
        if label == ref:
            return idx, src
    return None, None


def cmd_source_list(args):
    store = ConfigStore()
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)
    name = str(getattr(args, "name", "") or "").strip()
    project = store.resolve_project(name)
    if project is None:
        print(f"Error: Project '{name}' not found.")
        sys.exit(1)
    sources = explicit_sources(project)
    print(f"Sources for project '{name}':")
    for idx, src in enumerate(sources, start=1):
        primary = " (primary)" if getattr(src, "primary", False) else ""
        loc = getattr(src, "directory", "") or getattr(src, "repo", "") or "-"
        host = getattr(src, "host", "") or ""
        host_str = f" @ {host}" if host else " @ local"
        mount = getattr(src, "mount_path", "") or _source_mount_path(src, idx - 1)
        label = getattr(src, "name", "") or getattr(src, "project", "") or f"source-{idx}"
        print(f"  [{idx}] {label}{primary}: {loc}{host_str}  ->  {mount}")


def cmd_source_add(args):
    store = ConfigStore()
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)
    project_name = str(getattr(args, "name", "") or "").strip()
    project = store.load_project(project_name)
    if project is None:
        print(f"Error: Project '{project_name}' not found.")
        sys.exit(1)

    directory = str(getattr(args, "dir", None) or "").strip()
    repo = str(getattr(args, "repo", None) or "").strip()
    source_name = str(getattr(args, "source_name", None) or "").strip()
    mount_path = str(getattr(args, "mount_path", None) or "").strip()
    make_primary = bool(getattr(args, "primary", False))
    ssh_key = str(getattr(args, "ssh_key", None) or "").strip()
    host = str(getattr(args, "host", None) or "").strip()

    if directory and repo:
        print("Error: --dir and --repo are mutually exclusive.")
        sys.exit(1)
    if not directory and not repo:
        print("Error: Specify either --dir or --repo.")
        sys.exit(1)
    if directory:
        directory = str(Path(directory).expanduser().resolve())
        if not Path(directory).is_dir():
            print(f"Error: Directory does not exist: {directory}")
            sys.exit(1)

    sources = explicit_sources(project)

    if not source_name:
        if repo:
            source_name = repo.rstrip("/").split("/")[-1]
            if source_name.endswith(".git"):
                source_name = source_name[:-4]
        else:
            source_name = Path(directory).name

    existing_names = {(getattr(s, "name", "") or getattr(s, "project", "")) for s in sources}
    if source_name in existing_names:
        print(f"Error: Source '{source_name}' already exists in project '{project_name}'.")
        sys.exit(1)

    if not mount_path:
        mount_path = f"/home/dev/{source_name}"

    new_source = ProjectSourceSpec(
        project=project_name,
        name=source_name,
        directory=directory,
        repo=repo,
        host=host,
        ssh_private_key=ssh_key,
        mount_path=mount_path,
        primary=make_primary,
    )
    if make_primary:
        for src in sources:
            src.primary = False
    elif not any(getattr(s, "primary", False) for s in sources):
        new_source.primary = True

    sources.append(new_source)
    project.sources = sources
    sync_project_primary(project, sources)
    store.save_resource(project)

    primary_marker = " (primary)" if new_source.primary else ""
    loc = directory or repo
    print(f"Added source '{source_name}'{primary_marker} to project '{project_name}'.")
    print(f"  Location:   {loc}")
    print(f"  Mount:      {mount_path}")
    if host:
        print(f"  Host:       {host}")


def cmd_source_remove(args):
    store = ConfigStore()
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)
    project_name = str(getattr(args, "name", "") or "").strip()
    project = store.load_project(project_name)
    if project is None:
        print(f"Error: Project '{project_name}' not found.")
        sys.exit(1)

    sources = explicit_sources(project)
    if len(sources) <= 1:
        print(f"Error: Project '{project_name}' has only one source and cannot be reduced further.")
        sys.exit(1)

    source_ref = str(getattr(args, "source", "") or "").strip()
    target_idx, removed = _resolve_source(sources, source_ref)
    if target_idx is None:
        print(f"Error: Source '{source_ref}' not found in project '{project_name}'.")
        print("Use 'skua source list' to see available sources.")
        sys.exit(1)

    sources.pop(target_idx)
    if getattr(removed, "primary", False) and sources:
        sources[0].primary = True

    project.sources = sources
    sync_project_primary(project, sources)
    store.save_resource(project)

    label = getattr(removed, "name", "") or getattr(removed, "project", "") or source_ref
    print(f"Removed source '{label}' from project '{project_name}'.")
    if sources:
        primary = next((s for s in sources if getattr(s, "primary", False)), sources[0])
        p_label = getattr(primary, "name", "") or getattr(primary, "project", "") or "source-1"
        print(f"  Primary source is now: {p_label}")


def cmd_source_set_primary(args):
    store = ConfigStore()
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)
    project_name = str(getattr(args, "name", "") or "").strip()
    project = store.load_project(project_name)
    if project is None:
        print(f"Error: Project '{project_name}' not found.")
        sys.exit(1)

    sources = explicit_sources(project)
    source_ref = str(getattr(args, "source", "") or "").strip()
    target_idx, target = _resolve_source(sources, source_ref)
    if target_idx is None:
        print(f"Error: Source '{source_ref}' not found in project '{project_name}'.")
        sys.exit(1)

    for idx, src in enumerate(sources):
        src.primary = (idx == target_idx)

    project.sources = sources
    sync_project_primary(project, sources)
    store.save_resource(project)

    label = getattr(target, "name", "") or getattr(target, "project", "") or source_ref
    print(f"Source '{label}' is now primary for project '{project_name}'.")


def cmd_source(args):
    action = getattr(args, "action", None)
    dispatch = {
        "list": cmd_source_list,
        "add": cmd_source_add,
        "remove": cmd_source_remove,
        "set-primary": cmd_source_set_primary,
    }
    if not action or action not in dispatch:
        print("usage: skua source <action> [options]")
        print()
        print("actions:")
        print("  list <project>                List sources for a project")
        print("  add <project> --dir|--repo    Add a source to a project")
        print("  remove <project> <source>     Remove a source (by name or index)")
        print("  set-primary <project> <src>   Set a source as primary")
        sys.exit(1)
    dispatch[action](args)
