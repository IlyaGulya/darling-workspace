#!/usr/bin/env python3
"""Fail closed when proposed schema-v2 locks are not in an immutable closure.

This review-time contract deliberately validates object closure before any
authoring object database may be discarded or any tag can be published.  The
closure may be a local bare mirror during review or an independently cloned
hosted mirror during publication acceptance.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

import yaml


class ClosureError(RuntimeError):
    pass


def git(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode:
        raise ClosureError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def git_optional(git_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    if result.returncode not in {0, 1}:
        raise ClosureError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def require_object(git_dir: Path, oid: str, label: str) -> None:
    try:
        git(git_dir, "cat-file", "-e", f"{oid}^{{commit}}")
    except ClosureError as error:
        raise ClosureError(f"{label}: missing closure object {oid}") from error


def require_ref(git_dir: Path, ref: str, oid: str, label: str) -> None:
    actual = git(git_dir, "rev-parse", ref)
    if actual != oid:
        raise ClosureError(f"{label}: {ref} is {actual}, expected {oid}")


def require_clean_odb(git_dir: Path) -> None:
    if (git_dir / "objects" / "info" / "alternates").exists():
        raise ClosureError("closure has alternates")
    if (git_dir / "shallow").exists():
        raise ClosureError("closure is shallow")
    if git_optional(git_dir, "config", "--get", "extensions.partialClone"):
        raise ClosureError("closure is partial")
    if git(git_dir, "for-each-ref", "--format=%(refname)", "refs/replace"):
        raise ClosureError("closure has replace refs")
    git(git_dir, "fsck", "--full", "--no-dangling")


def verify_lock(git_dir: Path, path: Path) -> None:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        raise ClosureError(f"{path}: not schema v2")
    mirror = data.get("mirror")
    if not isinstance(mirror, dict):
        raise ClosureError(f"{path}: missing mirror")
    source = data.get("source_commit")
    ordered = data.get("ordered_commits")
    base = data.get("upstream", {}).get("base_commit")
    if not all(isinstance(value, str) and len(value) == 40 for value in (base, source)):
        raise ClosureError(f"{path}: malformed base/source")
    if not isinstance(ordered, list) or not ordered or ordered[-1] != source:
        raise ClosureError(f"{path}: malformed ordered source topology")
    require_object(git_dir, base, f"{path}: base")
    require_object(git_dir, source, f"{path}: source")
    require_ref(git_dir, mirror["base_ref"], mirror["base_oid"], f"{path}: base ref")
    require_ref(git_dir, mirror["source_ref"], mirror["source_oid"], f"{path}: source ref")
    for index, oid in enumerate(ordered, start=1):
        require_object(git_dir, oid, f"{path}: ordered[{index}]")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--closure", required=True, type=Path)
    parser.add_argument(
        "--closure-copy", action="append", default=[], type=Path,
        help="independent bare copy to validate against the same locks",
    )
    parser.add_argument(
        "--authoring-odb", type=Path,
        help=("authoring bare ODB that must still contain every base/source; "
              "requiring this argument also requires an independent closure copy"),
    )
    parser.add_argument("--lock", required=True, action="append", type=Path)
    args = parser.parse_args()
    try:
        closures = [args.closure, *args.closure_copy]
        if args.authoring_odb is not None and not args.closure_copy:
            raise ClosureError(
                "authoring ODB cannot be retired before an independent closure copy is verified"
            )
        if len({path.resolve() for path in closures}) != len(closures):
            raise ClosureError("closure copies must be distinct directories")
        for closure in closures:
            if not closure.is_dir() or not (closure / "objects").is_dir():
                raise ClosureError(f"closure is not a bare repository: {closure}")
            require_clean_odb(closure)
            for lock in args.lock:
                verify_lock(closure, lock)
        if args.authoring_odb is not None:
            if not args.authoring_odb.is_dir() or not (args.authoring_odb / "objects").is_dir():
                raise ClosureError(f"authoring ODB is not a bare repository: {args.authoring_odb}")
            require_clean_odb(args.authoring_odb)
            for lock in args.lock:
                data = yaml.safe_load(lock.read_text())
                require_object(args.authoring_odb, data["upstream"]["base_commit"], f"{lock}: authoring base")
                require_object(args.authoring_odb, data["source_commit"], f"{lock}: authoring source")
    except (ClosureError, OSError, KeyError, TypeError, yaml.YAMLError) as error:
        print(f"v6 publication closure contract: FAIL: {error}", file=sys.stderr)
        return 1
    print(
        f"v6 publication closure contract: PASS ({len(args.lock)} locks, "
        f"{len(closures)} independent closure copies)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
