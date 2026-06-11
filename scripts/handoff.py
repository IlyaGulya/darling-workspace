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

        head = git(repo, "rev-parse", "HEAD")
        base = public_base(repo, head)
        if not base:
            continue
        branch = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
        filename = ("root" if relative == "." else relative.replace("/", "__")) + ".bundle"
        bundle = output / filename
        result = subprocess.run(
            ["git", "-C", str(repo), "bundle", "create", str(bundle), "HEAD", f"^{base}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            records.append(
                {
                    "path": relative,
                    "branch": branch,
                    "head": head,
                    "base": base,
                    "bundle": filename,
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
        branch = record["branch"]
        head = record["head"]
        bundle = input_dir / record["bundle"]
        existing = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}", required=False)
        if existing and existing != head:
            raise SystemExit(f"{record['path']}: branch {branch} already differs")
        subprocess.run(
            ["git", "-C", str(repo), "fetch", str(bundle), f"HEAD:refs/heads/{branch}"],
            check=True,
        )
        subprocess.run(["git", "-C", str(repo), "switch", branch], check=True)
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
