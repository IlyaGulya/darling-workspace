"""Prove a cached runtime source forest is reused only when it is complete.

A runtime forest is materialized at a stable identity-keyed path and reused by
later runs, which is what makes cross-run ccache hits and incremental builds
possible. Reuse is only sound when the entry is complete and unchanged:

* a materialized forest must be recognized as reusable, including after profile
  application commits inside it, because that moves the forest HEAD away from
  the revision the worktree was created from;
* an entry without a marker, and an entry whose forest HEAD moved after the
  marker was written, must be rejected;
* discarding an entry must leave no worktree registration behind, or the next
  materialization fails with "missing but already registered worktree" instead
  of rebuilding the forest. The nested gitlinks that hydration creates are
  worktrees of their own repositories, so they are the case that breaks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_runtime_cache import source_entry, source_key  # noqa: E402
from source_worktree import _add_detached_worktree  # noqa: E402
from test_runtime_source import (  # noqa: E402
    RuntimeSourceMaterializer,
    record_runtime_source_marker,
)

GIT_ENV = {
    "GIT_AUTHOR_NAME": "contract",
    "GIT_AUTHOR_EMAIL": "contract@example.invalid",
    "GIT_COMMITTER_NAME": "contract",
    "GIT_COMMITTER_EMAIL": "contract@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
}
ENVIRONMENT = {**os.environ, **GIT_ENV}


def git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *arguments], cwd=cwd, env=ENVIRONMENT, capture_output=True, text=True, check=False
    )


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git("init", "--quiet", "--initial-branch=main", cwd=path)
    (path / "payload.txt").write_text("one\n")
    git("add", "payload.txt", cwd=path)
    git("commit", "--quiet", "-m", "seed", cwd=path)
    return path


def registrations(repo: Path, store: Path) -> list[str]:
    result = git("worktree", "list", "--porcelain", cwd=repo)
    return [
        line.removeprefix("worktree ")
        for line in result.stdout.splitlines()
        if line.startswith("worktree ") and str(store) in line
    ]


def main() -> int:
    materializer = RuntimeSourceMaterializer(SimpleNamespace())
    key = source_key({"contract": "runtime-source-reuse"})

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        store = root / "store"
        darling_repo = repository(root / "darling")
        nested_repo = repository(root / "nested")
        entry = source_entry(store, key)
        forest = entry / "darling"
        nested = forest / "src/external/Nested"

        assert not materializer._reusable_runtime_source(entry, key), (
            "an absent entry must not be reusable"
        )

        entry.mkdir(parents=True)
        added = git("worktree", "add", "--quiet", "--detach", str(forest), "HEAD", cwd=darling_repo)
        assert added.returncode == 0, added.stderr
        assert not materializer._reusable_runtime_source(entry, key), (
            "an entry without a completion marker must not be reusable"
        )

        # Profile application commits inside the forest, so the completion
        # marker must record the materialized HEAD rather than the revision the
        # worktree was created from.
        (forest / "payload.txt").write_text("patched\n")
        git("add", "payload.txt", cwd=forest)
        git("commit", "--quiet", "-m", "apply profile", cwd=forest)
        head = git("rev-parse", "HEAD", cwd=forest).stdout.strip()
        start = git("rev-parse", "HEAD", cwd=root / "darling").stdout.strip()
        assert head != start, "the materialized forest must have moved off the source revision"
        assert record_runtime_source_marker(entry, key, forest) == head, (
            "the completion marker must record the materialized HEAD, not the "
            "revision the worktree was created from"
        )
        assert materializer._reusable_runtime_source(entry, key), (
            "a complete forest marked at its materialized HEAD must be reusable"
        )

        (forest / "payload.txt").write_text("mutated\n")
        git("add", "payload.txt", cwd=forest)
        git("commit", "--quiet", "-m", "mutate", cwd=forest)
        assert not materializer._reusable_runtime_source(entry, key), (
            "a forest whose HEAD moved after the marker was written must not be reused"
        )

        materializer._discard_runtime_source(entry, darling_repo, set(), {})
        assert not entry.exists(), "a discarded entry must be gone"
        assert registrations(darling_repo, store) == [], (
            "discarding must unregister the forest worktree"
        )

        # Re-materializing the same key after a discard is the path a crashed
        # run takes; a leftover registration makes it fail, and the gitlink
        # worktrees created by hydration live in their own repositories.
        entry.mkdir(parents=True)
        again = git("worktree", "add", "--quiet", "--detach", str(forest), "HEAD", cwd=darling_repo)
        assert again.returncode == 0, (
            "a discarded entry must be materializable again: " + again.stderr.strip()
        )
        nested_added = git(
            "worktree", "add", "--quiet", "--detach", str(nested), "HEAD", cwd=nested_repo
        )
        assert nested_added.returncode == 0, nested_added.stderr
        (forest / "payload.txt").write_text("patched again\n")
        git("add", "payload.txt", cwd=forest)
        git("commit", "--quiet", "-m", "apply profile again", cwd=forest)
        assert record_runtime_source_marker(entry, key, forest) is not None
        assert materializer._reusable_runtime_source(entry, key)

        # Discarding must clear the nested registration too, and must leave the
        # key materializable afterwards.
        materializer._discard_runtime_source(entry, darling_repo, set(), {})
        assert not entry.exists()
        assert registrations(nested_repo, store) == [], (
            "discarding must unregister nested gitlink worktrees"
        )
        assert registrations(darling_repo, store) == []
        entry.mkdir(parents=True)
        third = git("worktree", "add", "--quiet", "--detach", str(forest), "HEAD", cwd=darling_repo)
        assert third.returncode == 0, third.stderr
        nested_third = git(
            "worktree", "add", "--quiet", "--detach", str(nested), "HEAD", cwd=nested_repo
        )
        assert nested_third.returncode == 0, nested_third.stderr

        # A forest deleted without unregistering leaves Git refusing the same
        # path with "missing but already registered worktree"; materialization
        # must recover and retry instead of failing every later run.
        import shutil

        stale = root / "stale"
        added = git("worktree", "add", "--quiet", "--detach", str(stale), "HEAD", cwd=darling_repo)
        assert added.returncode == 0, added.stderr
        shutil.rmtree(stale)
        refused = git("worktree", "add", "--quiet", "--detach", str(stale), "HEAD", cwd=darling_repo)
        assert refused.returncode != 0 and "already registered" in refused.stderr, refused.stderr
        _add_detached_worktree(darling_repo, stale, "HEAD")
        assert (stale / "payload.txt").is_file(), (
            "a stale registration must be pruned and the add retried"
        )

    print("PASS runtime-source-reuse-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
