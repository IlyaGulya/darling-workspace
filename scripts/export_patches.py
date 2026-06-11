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


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile")
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--manifest-repo", type=Path, default=Path(__file__).parents[1])
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
            ["git", "format-patch", "-1", "--stdout", commit],
            cwd=repo,
            check=True,
            stdout=subprocess.PIPE,
        ).stdout
        output = profile_dir / patch["path"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(content)
        patch["source-commit"] = commit
        patch["sha256sum"] = hashlib.sha256(content).hexdigest()

    manifest_path.write_text(yaml.safe_dump(data, sort_keys=False, width=1000))


if __name__ == "__main__":
    main()
