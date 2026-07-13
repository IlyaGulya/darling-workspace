#!/usr/bin/env python3
"""Generate the west spike manifest from the current base repo manifest."""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

FETCH_OVERRIDES = {
    # The upstream v2.8.3 ref no longer contains Darling's pinned commit.
    "darling/src/external/libressl-2.8.3": {
        "remote": "darling-next",
        "repo-path": "darling-libressl",
        "revision": "2a56b36b77a00573c53ccd8e6932eb136172c950",
        "reason": "darling-next-public-fork-preserves-pin",
    },
    "darling/src/external/nghttp2/third-party/mruby": {
        "remote": "mruby-upstream",
        "repo-path": "mruby",
        "reason": "third-party-upstream",
    },
    "darling/src/external/nghttp2/third-party/neverbleed": {
        "remote": "h2o-upstream",
        "repo-path": "neverbleed",
        "reason": "third-party-upstream",
    },
    "darling/src/external/xcbuild/ThirdParty/googletest": {
        "remote": "google-upstream",
        "repo-path": "googletest",
        "reason": "third-party-upstream",
    },
    "darling/src/external/xcbuild/ThirdParty/linenoise": {
        "remote": "antirez-upstream",
        "repo-path": "linenoise",
        "reason": "third-party-upstream",
    },
}


def project_id(path: str) -> str:
    if path == "darling":
        return "darling"
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", path).strip("-").lower()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--runner-revision", required=True)
    args = parser.parse_args()

    root = ET.parse(args.repo_manifest).getroot()
    projects = []
    for project in root.findall("project"):
        path = project.attrib["path"]
        override = FETCH_OVERRIDES.get(path, {})
        entry = {
            "name": project_id(path),
            "repo-path": override.get("repo-path", project.attrib["name"]),
            "path": path,
            "remote": override.get("remote", "darling"),
            "revision": override.get("revision", project.attrib["revision"]),
            "userdata": {
                "kind": "darling-source",
                "upstream-repository": project.attrib["name"],
            },
        }
        if override:
            entry["userdata"]["fetch-override-reason"] = override["reason"]
        projects.append(entry)

    projects.append(
        {
            "name": "darling-debug-runner",
            "path": "darling-debug-runner",
            "remote": "darling-next",
            "revision": args.runner_revision,
            "groups": ["private", "debug-tools"],
            "userdata": {
                "kind": "workspace-tool",
                "owner": "darling-next",
            },
        }
    )

    manifest = {
        "manifest": {
            "version": "0.13",
            "remotes": [
                {
                    "name": "darling",
                    "url-base": "https://github.com/darlinghq",
                },
                {
                    "name": "darling-next",
                    "url-base": "https://github.com/darling-next",
                },
                {
                    "name": "mruby-upstream",
                    "url-base": "https://github.com/mruby",
                },
                {
                    "name": "h2o-upstream",
                    "url-base": "https://github.com/h2o",
                },
                {
                    "name": "google-upstream",
                    "url-base": "https://github.com/google",
                },
                {
                    "name": "antirez-upstream",
                    "url-base": "https://github.com/antirez",
                },
            ],
            "projects": projects,
            "self": {
                "path": "darling-workspace",
                "west-commands": "west-commands.yml",
            },
        }
    }
    args.output.write_text(
        yaml.safe_dump(manifest, sort_keys=False, width=1000),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
