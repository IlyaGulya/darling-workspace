#!/usr/bin/env python3
"""Restore private refs in a West-managed Darling workspace."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(*args: str, cwd: Path, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def git(repo: Path, *args: str, capture: bool = False) -> str:
    return run("git", *args, cwd=repo, capture=capture)


def ensure_clean(repo: Path, nested_projects: bool = False) -> None:
    status_args = ["status", "--porcelain"]
    if nested_projects:
        status_args.extend(["--ignore-submodules=all", "--untracked-files=no"])
    status = git(repo, *status_args, capture=True)
    if status:
        raise SystemExit(f"{repo}: worktree is dirty")


def workspace_path(topdir: Path, relative: str) -> Path:
    return topdir / "darling" if relative == "." else topdir / "darling" / relative


def restore(topdir: Path, manifest_repo: Path) -> None:
    handoff = manifest_repo / "handoff"
    data = json.loads((handoff / "manifest.json").read_text())
    restored = 0

    for project in data["projects"]:
        repo = workspace_path(topdir, project["path"])
        ensure_clean(repo, nested_projects=project["path"] == ".")
        bundle = handoff / project["bundle"]
        for branch_record in project["branches"]:
            branch = branch_record["branch"]
            head = branch_record["head"]
            existing = subprocess.run(
                ["git", "rev-parse", "--verify", f"refs/heads/{branch}"],
                cwd=repo,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ).stdout.strip()
            if existing and existing != head:
                raise SystemExit(
                    f"{project['path']}: local {branch} differs from handoff "
                    f"({existing} != {head})"
                )
            source_ref = branch_record["source_ref"]
            git(
                repo,
                "fetch",
                str(bundle),
                f"{source_ref}:refs/heads/{branch}",
            )
            restored += 1

    print(f"restored {restored} private branches in {len(data['projects'])} projects")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topdir", required=True, type=Path)
    parser.add_argument("--manifest-repo", required=True, type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("restore")
    args = parser.parse_args()

    topdir = args.topdir.resolve()
    manifest_repo = args.manifest_repo.resolve()
    restore(topdir, manifest_repo)


if __name__ == "__main__":
    main()
