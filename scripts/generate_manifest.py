#!/usr/bin/env python3
"""Generate a repo manifest from a Darling checkout."""

from __future__ import annotations

import argparse
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse


def git(repo: Path, *args: str, required: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if required and result.returncode:
        raise SystemExit(f"git failed in {repo}: {' '.join(args)}")
    return result.stdout.strip()


def github_name(url: str) -> str:
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    else:
        path = urlparse(url).path.lstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name.removesuffix(".git")


def submodule_url(source: Path, relative: str) -> str:
    target = Path(relative)
    for parent in [target.parent, *target.parents]:
        base = source / parent
        modules = base / ".gitmodules"
        if not modules.is_file():
            continue
        local_path = str(target.relative_to(parent))
        entries = subprocess.run(
            ["git", "config", "-f", str(modules), "--get-regexp", r"^submodule\..*\.path$"],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
        for line in entries.splitlines():
            key, value = line.split(maxsplit=1)
            if value == local_path:
                url_key = key.removesuffix(".path") + ".url"
                return subprocess.check_output(
                    ["git", "config", "-f", str(modules), "--get", url_key],
                    text=True,
                ).strip()
    raise SystemExit(f"cannot find submodule URL for {relative}")


def projects(source: Path) -> list[tuple[str, str, str]]:
    output = git(source, "submodule", "status", "--recursive")
    result = [(".", git(source, "rev-parse", "HEAD"), git(source, "remote", "get-url", "origin"))]
    for line in output.splitlines():
        marker = line[0]
        fields = line[1:].split()
        if len(fields) >= 2:
            sha, relative = fields[:2]
            repo = source / relative
            if marker == "-":
                origin = submodule_url(source, relative)
            else:
                sha = git(repo, "rev-parse", "HEAD")
                origin = git(repo, "remote", "get-url", "origin")
            result.append((relative, sha, origin))
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    source = args.source.resolve()
    manifest = ET.Element("manifest")
    ET.SubElement(
        manifest,
        "remote",
        name="darling",
        fetch="https://github.com/darlinghq/",
    )
    ET.SubElement(
        manifest,
        "default",
        remote="darling",
        **{"sync-j": "16", "sync-tags": "false"},
    )

    seen: set[tuple[str, str]] = set()
    for relative, revision, origin in projects(source):
        name = github_name(origin)
        path = "darling" if relative == "." else f"darling/{relative}"
        key = (name, path)
        if key in seen:
            continue
        seen.add(key)

        attrs = {
            "name": name,
            "path": path,
            "revision": revision,
        }
        ET.SubElement(manifest, "project", attrs)

    ET.indent(manifest, space="  ")
    tree = ET.ElementTree(manifest)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(args.output, encoding="unicode", xml_declaration=True)
    with args.output.open("a", encoding="utf-8") as output:
        output.write("\n")


if __name__ == "__main__":
    main()
