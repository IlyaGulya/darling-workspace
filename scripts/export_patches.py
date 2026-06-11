#!/usr/bin/env python3
"""Refresh patch files and checksums from canonical topic branches."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
from pathlib import Path

import yaml


def source_repo(source_root: Path, module: str) -> Path:
    if module == "darling":
        return source_root
    return source_root / Path(module).relative_to("darling")


def format_patch_command(patch: dict, commit: str) -> list[str]:
    revision = (
        f"{patch['source-base']}..{commit}"
        if patch.get("source-base")
        else commit
    )
    command = [
        "git",
        "format-patch",
        "--stdout",
        "--no-signature",
        "--no-numbered",
        "--subject-prefix=PATCH",
        "--full-index",
        "--binary",
        "--no-renames",
    ]
    if not patch.get("source-base"):
        command.append("-1")
    command.append(revision)
    return command


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--manifest-repo", type=Path, default=Path(__file__).parents[1])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    profile_dir = args.manifest_repo.resolve() / "patches" / args.profile
    manifest_path = profile_dir / "patches.yml"
    data = yaml.safe_load(manifest_path.read_text())

    for patch in data["patches"]:
        repo = source_repo(args.source_root.resolve(), patch["module"])
        commit = subprocess.run(
            ["git", "rev-parse", patch["source-branch"]],
            cwd=repo,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        content = subprocess.run(
            format_patch_command(patch, commit),
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        output = profile_dir / patch["path"]
        checksum = hashlib.sha256(content).hexdigest()
        if args.check:
            if patch.get("source-commit") != commit:
                raise SystemExit(f"{patch['path']}: source commit drift")
            if patch.get("sha256sum") != checksum:
                raise SystemExit(f"{patch['path']}: checksum drift")
            if not output.is_file() or output.read_bytes() != content:
                raise SystemExit(f"{patch['path']}: exported patch drift")
        else:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
            patch["source-commit"] = commit
            patch["sha256sum"] = checksum

    if not args.check:
        manifest_path.write_text(yaml.safe_dump(data, sort_keys=False, width=1000))


if __name__ == "__main__":
    main()
