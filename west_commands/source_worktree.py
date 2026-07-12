"""Prepare reproducible, fully materialized Darling source worktrees.

Git worktrees do not populate submodules.  Fetching them from their remotes is
not reproducible in this workspace because some manifest revisions are local
patch-stack commits.  This module instead materializes every gitlink from the
canonical local checkout at the exact revision selected by the source tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable


class SourceWorktreeError(RuntimeError):
    """An isolated source worktree cannot be prepared safely."""


@dataclass(frozen=True)
class PreparedGitlink:
    """One nested worktree bound to an exact source-tree gitlink."""

    relative_path: str
    revision: str
    canonical_repo: str
    created: bool


RevisionResolver = Callable[[Path, str], str]
_GITLINK_CACHE: dict[tuple[str, str], tuple[tuple[str, str], ...]] = {}


def _git(repo: Path, *args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise SourceWorktreeError(
            f"{repo}: git {' '.join(args)} failed with rc={result.returncode}"
            f"{': ' + detail if detail else ''}"
        )
    return result.stdout.strip() if capture else ""


def _gitlinks(repo: Path, revision: str) -> list[tuple[str, str]]:
    """Return direct ``(name, sha)`` gitlinks in *revision*."""

    key = (str(repo.resolve()), revision)
    cached = _GITLINK_CACHE.get(key)
    if cached is not None:
        return list(cached)

    output = _git(repo, "ls-tree", "-r", "-z", revision, capture=True)
    entries: list[tuple[str, str]] = []
    for record in output.split("\0"):
        if not record:
            continue
        metadata, separator, name = record.partition("\t")
        if not separator:
            raise SourceWorktreeError(
                f"{repo}: malformed git ls-tree record for {revision!r}"
            )
        fields = metadata.split()
        if len(fields) != 3:
            raise SourceWorktreeError(
                f"{repo}: malformed git ls-tree metadata for {revision!r}"
            )
        mode, _kind, sha = fields
        if mode == "160000":
            entries.append((name, sha))
    _GITLINK_CACHE[key] = tuple(entries)
    return entries


def _is_git_worktree(path: Path) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def _require_commit(repo: Path, revision: str, relative_path: Path) -> None:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{revision}^{{commit}}"],
        cwd=repo,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode:
        raise SourceWorktreeError(
            f"{relative_path}: canonical repository {repo} does not contain "
            f"required commit {revision}. Restore the local manifest revision "
            "before preparing this source worktree."
        )


def _worktree_head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD", capture=True)


def _worktree_clean(path: Path) -> bool:
    return not _git(path, "status", "--porcelain", capture=True)


def _remove_created(entries: list[PreparedGitlink], source_root: Path) -> None:
    failures: list[str] = []
    for entry in reversed(entries):
        if not entry.created:
            continue
        target = source_root / entry.relative_path
        repo = Path(entry.canonical_repo)
        result = subprocess.run(
            ["git", "worktree", "remove", "--force", str(target)],
            cwd=repo,
            check=False,
            text=True,
            capture_output=True,
        )
        if result.returncode and target.exists():
            detail = (result.stdout + result.stderr).strip()
            failures.append(f"{target} (rc={result.returncode}){': ' + detail if detail else ''}")
    if failures:
        raise SourceWorktreeError(
            "failed to remove prepared source worktree(s): " + "; ".join(failures)
        )


def prepare_source_worktree(
    source_root: Path,
    canonical_root: Path,
    *,
    revision_for: RevisionResolver | None = None,
) -> list[PreparedGitlink]:
    """Hydrate every gitlink below *source_root* using local canonical refs.

    Existing nested worktrees are accepted only when they are clean and exactly
    match the requested revision.  The function never replaces a directory or
    symlink supplied by the caller.  On failure it removes only worktrees it
    created during this call.
    """

    source_root = source_root.resolve()
    canonical_root = canonical_root.resolve()
    if not _is_git_worktree(source_root):
        raise SourceWorktreeError(f"source root is not a Git worktree: {source_root}")
    if not _is_git_worktree(canonical_root):
        raise SourceWorktreeError(f"canonical root is not a Git worktree: {canonical_root}")

    entries: list[PreparedGitlink] = []

    def hydrate(
        source_repo: Path,
        canonical_repo: Path,
        source_revision: str,
        relative_parent: Path,
    ) -> None:
        for name, tree_revision in _gitlinks(source_repo, source_revision):
            relative_path = relative_parent / name
            requested_revision = (
                revision_for(relative_path, tree_revision)
                if revision_for is not None
                else tree_revision
            )
            canonical_child = canonical_repo / name
            target = source_root / relative_path
            if canonical_child.is_symlink() or not _is_git_worktree(canonical_child):
                raise SourceWorktreeError(
                    f"{relative_path}: canonical nested repository is unavailable at "
                    f"{canonical_child}; initialize the canonical workspace first."
                )
            _require_commit(canonical_child, requested_revision, relative_path)
            created = False
            if target.exists() or target.is_symlink():
                if target.is_symlink():
                    raise SourceWorktreeError(
                        f"{relative_path}: refusing to replace symlinked source directory {target}"
                    )
                if target.is_dir() and not any(target.iterdir()):
                    # Git creates empty placeholders for gitlinks in a freshly
                    # added superproject worktree. They carry no caller state.
                    target.rmdir()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _git(
                        canonical_child,
                        "worktree",
                        "add",
                        "--quiet",
                        "--detach",
                        str(target),
                        requested_revision,
                    )
                    created = True
                elif not _is_git_worktree(target):
                    raise SourceWorktreeError(
                        f"{relative_path}: refusing to replace non-Git source directory {target}"
                    )
                if not created and (
                    _worktree_head(target) != requested_revision or not _worktree_clean(target)
                ):
                    raise SourceWorktreeError(
                        f"{relative_path}: existing nested worktree is not clean at "
                        f"{requested_revision}; remove or repair it before preparation."
                    )
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _git(
                    canonical_child,
                    "worktree",
                    "add",
                    "--quiet",
                    "--detach",
                    str(target),
                    requested_revision,
                )
                created = True
            entry = PreparedGitlink(
                relative_path=str(relative_path),
                revision=requested_revision,
                canonical_repo=str(canonical_child),
                created=created,
            )
            entries.append(entry)
            hydrate(target, canonical_child, requested_revision, relative_path)

    try:
        hydrate(source_root, canonical_root, _worktree_head(source_root), Path())
        return entries
    except BaseException:
        _remove_created(entries, source_root)
        raise


def default_record_path(source_root: Path) -> Path:
    """Keep preparation state beside, rather than inside, a clean source tree."""

    root = source_root.resolve()
    return root.parent / f".west-source-worktree-{root.name}.json"


def write_record(
    record_path: Path,
    *,
    source_root: Path,
    canonical_root: Path,
    entries: list[PreparedGitlink],
) -> None:
    """Atomically persist the exact hydration identity for check and cleanup."""

    record_path = record_path.resolve()
    record_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "source_root": str(source_root.resolve()),
        "canonical_root": str(canonical_root.resolve()),
        "source_revision": _worktree_head(source_root.resolve()),
        "gitlinks": [asdict(entry) for entry in entries],
    }
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{record_path.name}.", dir=record_path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        Path(temporary).replace(record_path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_record(record_path: Path) -> dict:
    try:
        payload = json.loads(record_path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise SourceWorktreeError(f"cannot read source worktree record {record_path}: {error}") from error
    if payload.get("version") != 1 or not isinstance(payload.get("gitlinks"), list):
        raise SourceWorktreeError(f"invalid source worktree record: {record_path}")
    return payload


def verify_record(record_path: Path, *, build_dir: Path | None = None) -> dict:
    """Verify exact nested revisions and optional CMake source/build identity."""

    payload = load_record(record_path)
    source_root = Path(payload["source_root"])
    if not _is_git_worktree(source_root):
        raise SourceWorktreeError(f"recorded source root is unavailable: {source_root}")
    if _worktree_head(source_root) != payload.get("source_revision") or not _worktree_clean(source_root):
        raise SourceWorktreeError(f"source root is no longer clean at its recorded revision: {source_root}")
    for item in payload["gitlinks"]:
        entry = PreparedGitlink(**item)
        target = source_root / entry.relative_path
        if target.is_symlink() or not _is_git_worktree(target):
            raise SourceWorktreeError(f"recorded nested source is unavailable: {target}")
        if _worktree_head(target) != entry.revision or not _worktree_clean(target):
            raise SourceWorktreeError(
                f"recorded nested source is not clean at {entry.revision}: {target}"
            )
    if build_dir is not None:
        cache = build_dir / "CMakeCache.txt"
        if not cache.is_file():
            raise SourceWorktreeError(f"not a configured CMake build directory: {build_dir}")
        source_line = next(
            (
                line.split("=", 1)[1]
                for line in cache.read_text(errors="replace").splitlines()
                if line.startswith("CMAKE_HOME_DIRECTORY:") and "=" in line
            ),
            None,
        )
        if source_line is None or Path(source_line).resolve() != source_root.resolve():
            raise SourceWorktreeError(
                f"CMake build {build_dir} is not configured for recorded source {source_root}"
            )
    return payload


def cleanup_record(record_path: Path) -> None:
    """Remove exactly the nested worktrees created by one preparation record."""

    payload = load_record(record_path)
    source_root = Path(payload["source_root"])
    entries = [PreparedGitlink(**item) for item in payload["gitlinks"]]
    _remove_created(entries, source_root)
    record_path.unlink(missing_ok=True)
