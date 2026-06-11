#!/usr/bin/env python3
"""Pack and restore unpublished Darling commits without pushing them."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

from generate_manifest import git, public_base


def initialized_projects(source: Path):
    yield ".", source
    output = git(source, "submodule", "status", "--recursive")
    for line in output.splitlines():
        if line.startswith("-"):
            continue
        fields = line[1:].split()
        if len(fields) >= 2:
            relative = fields[1]
            yield relative, source / relative


def private_branches(repo: Path, include_remote_only: bool = False) -> list[dict[str, str]]:
    origin_url = git(repo, "remote", "get-url", "origin", required=False).lower()
    personal_origin = "ilyagulya" in origin_url
    output = git(
        repo,
        "for-each-ref",
        "--format=%(refname:short)\t%(objectname)\t%(upstream:short)",
        "refs/heads",
    )
    branches = []
    for line in output.splitlines():
        branch, head, upstream = (line.split("\t") + ["", ""])[:3]
        if branch in {"main", "master"}:
            remote_head = git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{branch}",
                required=False,
            )
            if remote_head == head:
                continue
        if upstream:
            upstream_head = git(
                repo,
                "rev-parse",
                "--verify",
                upstream,
                required=False,
            )
            if upstream_head == head and (
                not personal_origin
                or not branch.startswith(("fix/", "experiment/", "backup/"))
            ):
                continue
        branches.append(
            {
                "branch": branch,
                "head": head,
                "upstream": upstream,
                "source_ref": f"refs/heads/{branch}",
            }
        )

    if not include_remote_only:
        return branches

    local_names = {item["branch"] for item in branches}
    remote_output = git(
        repo,
        "for-each-ref",
        "--format=%(refname:strip=3)\t%(objectname)",
        "refs/remotes/origin",
    )
    for line in remote_output.splitlines():
        branch, head = line.split("\t")
        if branch in {"HEAD", "main", "master"} or branch in local_names:
            continue
        if not branch.startswith(("fix/", "experiment/", "backup/")):
            continue
        branches.append(
            {
                "branch": branch,
                "head": head,
                "upstream": f"origin/{branch}",
                "source_ref": f"refs/remotes/origin/{branch}",
            }
        )
    return branches


def pack(source: Path, output: Path) -> None:
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    records = []
    dirty = []

    for relative, repo in initialized_projects(source):
        changes = git(repo, "status", "--porcelain")
        if changes:
            dirty.append(relative)

        branches = private_branches(repo, include_remote_only=relative == ".")
        if not branches:
            continue
        filename = ("root" if relative == "." else relative.replace("/", "__")) + ".bundle"
        bundle = output / filename
        refs = [item["source_ref"] for item in branches]
        exclusions = []
        for default_branch in ("main", "master"):
            base = git(
                repo,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{default_branch}",
                required=False,
            )
            if base:
                exclusions.append(f"^{base}")
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "bundle",
                "create",
                str(bundle),
                *refs,
                *exclusions,
            ],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            records.append(
                {
                    "path": relative,
                    "bundle": filename,
                    "branches": branches,
                }
            )

    (output / "manifest.json").write_text(
        json.dumps({"version": 1, "projects": records}, indent=2) + "\n"
    )
    print(f"packed {len(records)} repositories into {output}")
    if dirty:
        print("warning: uncommitted changes are not included:")
        for relative in dirty:
            print(f"  {relative}")


def restore(source: Path, input_dir: Path) -> None:
    data = json.loads((input_dir / "manifest.json").read_text())
    for record in data["projects"]:
        repo = source if record["path"] == "." else source / record["path"]
        bundle = input_dir / record["bundle"]
        for branch_record in record["branches"]:
            branch = branch_record["branch"]
            head = branch_record["head"]
            existing = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", required=False)
            if existing and existing != head:
                raise SystemExit(f"{record['path']}: branch {branch} already differs")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "fetch",
                    str(bundle),
                    f"{branch_record['source_ref']}:refs/heads/{branch}",
                ],
                check=True,
            )
            print(f"restored {record['path']} -> {branch}")


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("pack", "restore"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--source", required=True, type=Path)
        subparser.add_argument("--bundles", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "pack":
        pack(args.source.resolve(), args.bundles.resolve())
    else:
        restore(args.source.resolve(), args.bundles.resolve())


if __name__ == "__main__":
    main()
