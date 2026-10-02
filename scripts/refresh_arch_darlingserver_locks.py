#!/usr/bin/env python3
"""Rewrite the arch profile's darlingserver -profile-v6 locks to match the exported patches.

WHY THIS EXISTS: `west patch export` reports that these locks are stale and says plainly that "neither has a
refresh command today", so the only way to bring the arch profile back to a materializable state is to write the
schema-v2 lock fields from values that are read from an authority and checked before they are written. Every
field comes from the repository, never from a guess:

    upstream.base_commit / mirror.base_oid / mirror.base_ref   the source commit's PARENT (and its bases tag)
    source_commit / mirror.source_oid / mirror.source_ref      the entry's `source-commit` in patches.yml
    ordered_commits                                            exactly [source]
    expected_tree                                              `git rev-parse <source>^{tree}`

WHY THE BASE IS THE PARENT AND NOT THE PATCH'S AUTHORING BASE: schema-v2's preflight requires
`rev-list --count upstream.base_commit..source_commit == len(ordered_commits)`, and the replay `git am`s exactly
`ordered_commits`. MEASURED: a0-arch-redesign's patch is authored against a base forty commits behind its tip, so a
lock built on that base demands forty commits, one of which does not apply on this profile's lineage. Every
well-formed lock in this repository uses the tip's parent, which is the shape a replay can prove.

The script refuses to write a lock whose tags are absent locally, so it can never publish an anchor that does not
exist. Run without `--write` to see the plan.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

WS = pathlib.Path(__file__).resolve().parent.parent
ROOT = WS.parent
REPO = ROOT / "darling" / "src" / "external" / "darlingserver"
PATCHES = WS / "patches" / "arch" / "patches.yml"
LOCKS = WS / "locks" / "patch-stack"


def git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True)
    return out.stdout.strip()


def git_ok(*args: str) -> bool:
    """Run git for a yes/no question: a non-zero exit is an answer, not an exception."""
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True).returncode == 0


def main(argv: list[str]) -> int:
    write = "--write" in argv[1:]
    rows: list[tuple[str, str]] = []
    for block in re.split(r"\n(?=- path: )", PATCHES.read_text()):
        if "module: darling/src/external/darlingserver" not in block:
            continue
        name = re.search(r"- path: darlingserver/(\S+)\.patch", block)
        source = re.search(r"source-commit: ([0-9a-f]{40})", block)
        if name and source:
            rows.append((name.group(1), source.group(1)))

    written = 0
    for name, source in rows:
        lock = LOCKS / f"darlingserver-{name}-profile-v6.yml"
        if not lock.exists():
            print(f"SKIP {name}: no {lock.name}")
            continue
        base = git("rev-parse", f"{source}^")
        base_tag = f"refs/tags/patch-stack/v1/bases/{base}"
        source_tag = f"refs/tags/patch-stack/v1/sources/{source}"
        missing = [ref for ref in (base_tag, source_tag) if not git_ok("rev-parse", "--verify", "--quiet", ref)]
        if missing:
            print(f"REFUSE {name}: not present locally: {'; '.join(missing)}")
            return 3
        if git("rev-list", "--count", f"{base}..{source}") != "1":
            print(f"REFUSE {name}: {base[:10]}..{source[:12]} is not a single commit")
            return 3
        tree = git("rev-parse", f"{source}^{{tree}}")
        body = (
            "schema_version: 2\n"
            "project: {name: darlingserver, path: .}\n"
            "upstream: {url: https://github.com/darlinghq/darlingserver.git, base_commit: %s}\n"
            "mirror:\n"
            "  url: https://github.com/darling-next/darlingserver.git\n"
            "  base_ref: %s\n"
            "  base_oid: %s\n"
            "  source_ref: %s\n"
            "  source_oid: %s\n"
            "source_commit: %s\n"
            "ordered_commits: [%s]\n"
            "expected_tree: %s\n"
        ) % (base, base_tag, base, source_tag, source, source, source, tree)
        if write:
            lock.write_text(body)
            written += 1
        print(f"{'WROTE' if write else 'WOULD-WRITE'} {lock.name} base={base[:10]} source={source[:12]} tree={tree[:12]}")
    print(f"entries={len(rows)} written={written}" if write else f"entries={len(rows)} (dry run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
