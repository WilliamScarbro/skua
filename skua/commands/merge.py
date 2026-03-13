# SPDX-License-Identifier: BUSL-1.1
"""skua merge — create a composite project from existing projects."""

import sys

from skua.config import ConfigStore, Project
from skua.config.resources import ProjectImageSpec, ProjectSourceSpec
from skua.docker import _project_mount_path


def _merge_unique(items: list) -> list:
    out = []
    seen = set()
    for item in items or []:
        value = str(item).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _explicit_sources(project) -> list:
    sources = list(getattr(project, "sources", []) or [])
    if sources:
        return sources
    return [
        ProjectSourceSpec(
            project=project.name,
            name=project.name,
            directory=project.directory,
            repo=project.repo,
            host=getattr(project, "host", "") or "",
            ssh_private_key=getattr(project.ssh, "private_key", "") or "",
            mount_path=_project_mount_path(project),
            primary=True,
        )
    ]


def cmd_merge(args):
    store = ConfigStore()
    if not store.is_initialized():
        print("Skua is not initialized. Run 'skua init' first.")
        sys.exit(1)

    name = str(getattr(args, "name", "") or "").strip()
    parent_names = [str(p).strip() for p in list(getattr(args, "projects", []) or []) if str(p).strip()]
    master_name = str(getattr(args, "master", "") or "").strip() or (parent_names[0] if parent_names else "")

    if len(parent_names) < 2:
        print("Error: Provide at least two projects to merge.")
        sys.exit(1)
    if store.load_project(name) is not None:
        print(f"Error: Project '{name}' already exists.")
        sys.exit(1)
    if master_name not in parent_names:
        print(f"Error: Master project '{master_name}' must be one of: {', '.join(parent_names)}")
        sys.exit(1)

    parents = []
    for parent_name in parent_names:
        project = store.resolve_project(parent_name)
        if project is None:
            print(f"Error: Project '{parent_name}' not found.")
            sys.exit(1)
        parents.append(project)

    parent_map = {project.name: project for project in parents}
    master = parent_map[master_name]

    hosts = {str(getattr(project, "host", "") or "").strip() for project in parents}
    if len(hosts) > 1:
        print("Error: Cannot merge projects from different hosts.")
        print(f"  Hosts: {', '.join(sorted(h or 'LOCAL' for h in hosts))}")
        sys.exit(1)

    merged_sources = []
    seen_sources = set()
    for parent in parents:
        for source in _explicit_sources(parent):
            key = (
                str(getattr(source, "directory", "") or "").strip(),
                str(getattr(source, "repo", "") or "").strip(),
                str(getattr(source, "host", "") or "").strip(),
                str(getattr(source, "mount_path", "") or "").strip(),
            )
            if key in seen_sources:
                continue
            seen_sources.add(key)
            merged_sources.append(
                ProjectSourceSpec(
                    project=getattr(source, "project", "") or parent.name,
                    name=getattr(source, "name", "") or parent.name,
                    directory=getattr(source, "directory", "") or "",
                    repo=getattr(source, "repo", "") or "",
                    host=getattr(source, "host", "") or "",
                    ssh_private_key=getattr(source, "ssh_private_key", "") or "",
                    mount_path=getattr(source, "mount_path", "") or _project_mount_path(parent),
                    primary=bool(parent.name == master_name and getattr(source, "primary", False)),
                )
            )

    if not any(source.primary for source in merged_sources):
        merged_sources[0].primary = True
    primary_source = next(source for source in merged_sources if source.primary)

    extra_packages = []
    extra_commands = []
    for parent in parents:
        extra_packages.extend(list(getattr(parent.image, "extra_packages", []) or []))
        extra_commands.extend(list(getattr(parent.image, "extra_commands", []) or []))

    merged = Project(
        name=name,
        directory=primary_source.directory,
        repo=primary_source.repo,
        host=str(getattr(master, "host", "") or ""),
        environment=master.environment,
        security=master.security,
        agent=master.agent,
        credential=master.credential,
        git=master.git,
        ssh=master.ssh,
        image=ProjectImageSpec(
            base_image=str(getattr(master.image, "base_image", "") or ""),
            from_image=str(getattr(master.image, "from_image", "") or ""),
            extra_packages=_merge_unique(extra_packages),
            extra_commands=_merge_unique(extra_commands),
            version=int(getattr(master.image, "version", 0) or 0),
        ),
        sources=merged_sources,
        master_project=master_name,
    )

    store.save_resource(merged)

    print(f"Project '{name}' created.")
    print(f"  Master:      {master_name}")
    print(f"  Sources:     {len(merged_sources)}")
    print(f"  Environment: {merged.environment}")
    print(f"  Security:    {merged.security}")
    print(f"  Agent:       {merged.agent}")
    if merged.image.extra_packages:
        print(f"  Packages:    {', '.join(merged.image.extra_packages)}")
    if merged.image.extra_commands:
        print(f"  Commands:    {len(merged.image.extra_commands)} merged")
    print(f"\nRun with: skua run {name}")
