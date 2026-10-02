#!/usr/bin/env python3
"""Rewrite (or create) a profile's -profile-v6 locks for ONE module, from values read out of the repository.

WHY THIS EXISTS: `west patch export` reports that these locks are stale and says plainly that "neither has a
refresh command today", so the only way to bring a profile back to a materializable state is to write the
schema-v2 lock fields from an authority and check each one before writing it. Every field comes from the
repository, never from a guess:

    upstream.base_commit / mirror.base_oid / mirror.base_ref   the source commit's PARENT (and its bases tag)
    source_commit / mirror.source_oid / mirror.source_ref      the entry's `source-commit` in patches.yml
    ordered_commits                                            exactly [source]
    expected_tree                                              `git rev-parse <source>^{tree}`

WHY THE BASE IS THE PARENT AND NOT THE PATCH'S AUTHORING BASE: schema-v2's preflight requires
`rev-list --count upstream.base_commit..source_commit == len(ordered_commits)`, and the replay `git am`s exactly
`ordered_commits`. MEASURED: a0-arch-redesign's patch is authored against a base forty commits behind its tip, so a
lock built on that base demands forty commits, one of which does not apply on this profile's lineage. Every
well-formed lock in this repository uses the tip's parent, which is the shape a replay can prove.

WHY THE MODULE IS AN ARGUMENT (dar-b5pe): the first version of this tool knew only
`darling/src/external/darlingserver`, so it could repair that module's entries and then stopped -- while the arch
profile also carries `darling` entries whose locks were stale the same way. MEASURED: after the darlingserver
entries were repaired, `west test --profile arch --materialize-profile` failed on
`darling/mldr-compact-fd-band.patch` with the identical "immutable replay tree ... differs" shape. A tool that
repairs one module and calls the profile repaired is a tool that reports success one module early.

The script refuses to write a lock whose tags are absent locally, so it can never publish an anchor that does not
exist. Run without `--write` to see the plan.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import subprocess
import sys

WS = pathlib.Path(__file__).resolve().parent.parent
ROOT = WS.parent
LOCKS = WS / "locks" / "patch-stack"


def make_git(repo: pathlib.Path):
    def git(*args: str) -> str:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def git_ok(*args: str) -> bool:
        """Run git for a yes/no question: a non-zero exit is an answer, not an exception."""
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True).returncode == 0

    return git, git_ok


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="arch", help="profile whose patches.yml is read (default: arch)")
    parser.add_argument("--module", required=True, help="module path, e.g. darling or darling/src/external/darlingserver")
    parser.add_argument("--upstream", help="override the upstream URL (default: darlinghq/<prefix>)")
    parser.add_argument("--mirror", help="override the mirror URL (default: darling-next/<prefix>)")
    parser.add_argument("--write", action="store_true", help="write the locks; without it, print the plan")
    args = parser.parse_args(argv[1:])

    repo = ROOT / args.module
    patches = WS / "patches" / args.profile / "patches.yml"
    if not (repo / ".git").exists() and not (repo / ".git").is_file():
        print(f"REFUSE: {repo} is not a git checkout")
        return 2
    if not patches.is_file():
        print(f"REFUSE: {patches} does not exist")
        return 2
    git, git_ok = make_git(repo)

    rows: list[tuple[str, str, str]] = []
    module_pattern = re.compile(rf"^\s*module: {re.escape(args.module)}\s*$", re.M)
    for block in re.split(r"\n(?=- path: )", patches.read_text()):
        if not module_pattern.search(block):
            continue
        m = re.search(r"- path: (\S+)/(\S+)\.patch", block)
        source = re.search(r"source-commit: ([0-9a-f]{40})", block)
        if m and source:
            rows.append((m.group(1), m.group(2), source.group(1)))

    written = 0
    for prefix, name, source in rows:
        lock = LOCKS / f"{prefix}-{name}-profile-v6.yml"
        upstream = args.upstream or f"https://github.com/darlinghq/{prefix}.git"
        mirror = args.mirror or f"https://github.com/darling-next/{prefix}.git"
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
            f"project: {{name: {prefix}, path: .}}\n"
            f"upstream: {{url: {upstream}, base_commit: {base}}}\n"
            "mirror:\n"
            f"  url: {mirror}\n"
            f"  base_ref: {base_tag}\n"
            f"  base_oid: {base}\n"
            f"  source_ref: {source_tag}\n"
            f"  source_oid: {source}\n"
            f"source_commit: {source}\n"
            f"ordered_commits: [{source}]\n"
            f"expected_tree: {tree}\n"
        )
        if args.write:
            lock.write_text(body)
            written += 1
        print(
            f"{'WROTE' if args.write else 'WOULD-WRITE'} {lock.name} "
            f"base={base[:10]} source={source[:12]} tree={tree[:12]}"
        )
    print(f"profile={args.profile} module={args.module} entries={len(rows)} written={written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
