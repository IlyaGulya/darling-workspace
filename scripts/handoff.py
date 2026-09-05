#!/usr/bin/env python3
"""Pack and restore unpublished Darling commits without pushing them."""

from __future__ import annotations

import fcntl
import argparse
import json
import os
import shutil
import subprocess
import stat
import sys
from pathlib import Path

from generate_manifest import git, public_base, run_bounded


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


def private_branches(
    repo: Path, include_remote_only: bool = False, *, remote: str = "origin"
) -> list[dict[str, str]]:
    origin_url = git(repo, "remote", "get-url", remote, required=False).lower()
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
                f"refs/remotes/{remote}/{branch}",
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
        f"refs/remotes/{remote}",
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
                "upstream": f"{remote}/{branch}",
                "source_ref": f"refs/remotes/{remote}/{branch}",
            }
        )
    return branches


def bundle_heads(bundle: Path) -> dict[str, str]:
    if not bundle.exists():
        return {}
    try:
        result = run_bounded(["git", "bundle", "list-heads", str(bundle)])
    except subprocess.TimeoutExpired:
        return {}
    if result.returncode != 0 or result.overflow:
        return {}
    heads = {}
    for line in result.stdout.splitlines():
        head, ref = (line.split(maxsplit=1) + [""])[:2]
        if ref:
            heads[ref] = head
    return heads


def expected_bundle_heads(branches: list[dict[str, str]]) -> dict[str, str]:
    return {item["source_ref"]: item["head"] for item in branches}


def bundle_filename(relative: str) -> str:
    return ("root" if relative == "." else relative.replace("/", "__")) + ".bundle"


def write_package(
    projects: list[tuple[str, Path, list[dict[str, str]], list[str]]],
    output: Path,
) -> list[dict[str, object]]:
    """Write and verify a complete bundle package in a new directory."""
    if output.exists():
        raise RuntimeError(f"bundle staging directory already exists: {output}")
    output.mkdir(parents=True)
    records: list[dict[str, object]] = []
    filenames: set[str] = set()
    for relative, repo, branches, exclusions in projects:
        if not branches:
            continue
        filename = bundle_filename(relative)
        if filename in filenames:
            raise RuntimeError(f"bundle filename collision: {filename}")
        filenames.add(filename)
        bundle = output / filename
        refs = [item["source_ref"] for item in branches]
        try:
            result = run_bounded(
                [
                    "git",
                    "-C",
                    str(repo),
                    "bundle",
                    "create",
                    str(bundle),
                    *refs,
                    *exclusions,
                ]
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError(f"{relative}: git bundle create timed out") from error
        if result.overflow:
            raise RuntimeError(f"{relative}: git bundle create output limit exceeded")
        if result.returncode:
            detail = (result.stderr.strip() or result.stdout.strip() or "unknown error")[:4096]
            raise RuntimeError(f"{relative}: git bundle create failed: {detail}")
        expected = expected_bundle_heads(branches)
        observed = bundle_heads(bundle)
        if observed != expected:
            raise RuntimeError(
                f"{relative}: bundle heads differ: expected {expected}, observed {observed}"
            )
        records.append(
            {
                "path": relative,
                "bundle": filename,
                "branches": branches,
            }
        )
    (output / "manifest.json").write_text(
        json.dumps({"version": 1, "projects": records}, indent=2) + "\n",
        encoding="utf-8",
    )
    return records


def _fsync_tree(root: Path) -> None:
    directories = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
        elif path.is_dir() and not path.is_symlink():
            directories.append(path)
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _pack_transaction_paths(output: Path) -> tuple[Path, Path, Path, Path]:
    state = output.parent.parent / f".{output.parent.name}.{output.name}.pack-transaction"
    return state, state / "journal.json", state / "backup", state / "staged"


def _pack_write_journal(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    _fsync_directory(path.parent)


def _pack_remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _pack_cleanup(state: Path, journal: Path, backup: Path, staged: Path) -> None:
    for path in (backup, staged):
        _pack_remove(path)
    if journal.exists():
        journal.unlink()
    temporary = journal.with_name(journal.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    _fsync_directory(state)


def _recover_pack(output: Path, state: Path, journal: Path, backup: Path, staged: Path) -> None:
    if not journal.exists():
        _pack_cleanup(state, journal, backup, staged)
        return
    payload = json.loads(journal.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or payload.get("output") != str(output):
        raise RuntimeError(f"invalid handoff pack recovery journal: {journal}")
    status = payload.get("state")
    if status == "prepared":
        if backup.exists():
            _pack_remove(output)
            os.replace(backup, output)
            _fsync_directory(output.parent)
        elif not payload.get("existed"):
            _pack_remove(output)
            _fsync_directory(output.parent)
        elif not output.exists():
            raise RuntimeError(f"handoff pack backup is missing: {backup}")
    elif status != "committed":
        raise RuntimeError(f"invalid handoff pack recovery state: {status}")
    _pack_cleanup(state, journal, backup, staged)


def pack(source: Path, output: Path) -> None:
    projects = []
    dirty = []
    for relative, repo in initialized_projects(source):
        changes = git(repo, "status", "--porcelain", "--ignore-submodules=all")
        if changes:
            dirty.append(relative)
        branches = private_branches(repo, include_remote_only=relative == ".")
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
        projects.append((relative, repo, branches, exclusions))
    output.parent.mkdir(parents=True, exist_ok=True)
    state, journal, backup, staged = _pack_transaction_paths(output)
    state.mkdir(mode=0o700, exist_ok=True)
    state_metadata = state.lstat()
    if (
        state.is_symlink()
        or not stat.S_ISDIR(state_metadata.st_mode)
        or state_metadata.st_uid != os.getuid()
        or stat.S_IMODE(state_metadata.st_mode) & 0o077
    ):
        raise RuntimeError(f"unsafe handoff pack transaction state: {state}")
    _fsync_directory(state.parent)
    lock_path = state / "lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    lock_metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(lock_metadata.st_mode)
        or lock_metadata.st_uid != os.getuid()
        or stat.S_IMODE(lock_metadata.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise RuntimeError(f"unsafe handoff pack lock: {lock_path}")
    with os.fdopen(descriptor, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _recover_pack(output, state, journal, backup, staged)
        records = write_package(projects, staged)
        _fsync_tree(staged)
        payload: dict[str, object] = {
            "version": 1,
            "state": "prepared",
            "output": str(output),
            "existed": output.exists(),
        }
        _pack_write_journal(journal, payload)
        try:
            if output.exists():
                os.replace(output, backup)
                _fsync_directory(output.parent)
                _fsync_directory(state)
            if os.environ.get("DW_HANDOFF_PACK_CRASH_AFTER_DISPLACE") == "1":
                os._exit(87)
            os.replace(staged, output)
            _fsync_directory(output.parent)
            payload["state"] = "committed"
            _pack_write_journal(journal, payload)
        except BaseException:
            _recover_pack(output, state, journal, backup, staged)
            raise
        try:
            _pack_cleanup(state, journal, backup, staged)
        except OSError as error:
            print(f"warning: committed handoff pack cleanup failed: {error}", file=sys.stderr)
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
            result = run_bounded(
                [
                    "git",
                    "-C",
                    str(repo),
                    "fetch",
                    str(bundle),
                    f"{branch_record['source_ref']}:refs/heads/{branch}",
                ]
            )
            if result.overflow:
                raise RuntimeError(f"{record['path']}: git fetch output limit exceeded")
            if result.returncode:
                raise subprocess.CalledProcessError(result.returncode, "git fetch")
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
