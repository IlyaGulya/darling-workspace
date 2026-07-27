#!/usr/bin/env python3
"""Composition prerequisites are tree contracts, never replay-commit refs."""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

import patch_stack_lock_first as lock_first
import patch_stack_profile_composition as composition


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    if result.returncode:
        raise AssertionError(f"git {' '.join(args)} failed ({result.returncode}): {result.stderr}")
    return result.stdout.strip()


def commit_env(name: str, email: str) -> dict[str, str]:
    env = os.environ.copy()
    env.update({"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email})
    return env


def lock_doc(mirror: Path, base: str, source: str, tree: str) -> dict:
    return {
        "schema_version": 2,
        "project": {"name": "darling", "path": "."},
        "upstream": {"url": mirror.as_uri(), "base_commit": base},
        "mirror": {
            "url": mirror.as_uri(),
            "base_ref": f"refs/tags/patch-stack/v1/bases/{base}",
            "base_oid": base,
            "source_ref": f"refs/tags/patch-stack/v1/sources/{source}",
            "source_oid": source,
        },
        "source_commit": source,
        "ordered_commits": [source],
        "expected_tree": tree,
    }


def write(path: Path, payload: dict) -> None:
    path.write_text(yaml.safe_dump(payload, sort_keys=False))


def assert_parent_gitlink_normalization(root: Path) -> None:
    """A parent boundary accepts generated gitlink OIDs, never child content drift."""
    parent, child = root / "parent", root / "child"
    git(root, "init", "-q", str(parent))
    git(root, "init", "-q", str(child))
    for repo in (parent, child):
        git(repo, "config", "user.name", "Contract")
        git(repo, "config", "user.email", "contract@example.invalid")
    (child / "child.txt").write_text("stable child content\n")
    git(child, "add", "child.txt")
    git(child, "commit", "-qm", "child source")
    first_child = git(child, "rev-parse", "HEAD")
    child_tree = git(child, "rev-parse", "HEAD^{tree}")
    second_child = git(child, "commit-tree", child_tree, "-p", first_child, "-m", "generated child integration",
                       env=commit_env("Generated", "generated@example.invalid"))
    git(child, "update-ref", "HEAD", second_child)

    git(parent, "read-tree", "--empty")
    git(parent, "update-index", "--add", "--cacheinfo", f"160000,{first_child},child")
    expected_tree = git(parent, "write-tree")
    expected_commit = git(parent, "commit-tree", expected_tree, "-m", "expected parent")
    git(parent, "update-ref", "HEAD", expected_commit)
    git(parent, "update-index", "--add", "--cacheinfo", f"160000,{second_child},child")
    actual_tree = git(parent, "write-tree")
    actual_commit = git(parent, "commit-tree", actual_tree, "-p", expected_commit, "-m", "generated parent")
    git(parent, "update-ref", "HEAD", actual_commit)

    expected = {"darling": expected_tree, "darling/child": child_tree}
    repos = {"darling": parent, "darling/child": child}
    composition.verify_integration("darling", parent, expected_tree, expected, repos)

    (parent / "unexpected.txt").write_text("ordinary parent mutation\n")
    git(parent, "add", "unexpected.txt")
    git(parent, "commit", "-qm", "ordinary parent mutation")
    try:
        composition.verify_integration("darling", parent, expected_tree, expected, repos)
    except composition.ProfileCompositionError:
        pass
    else:
        raise AssertionError("ordinary parent content was normalized as a gitlink")

    git(parent, "reset", "--hard", "-q", actual_commit)
    (child / "child.txt").write_text("drifted child content\n")
    git(child, "commit", "-am", "child drift")
    try:
        composition.verify_integration("darling", parent, expected_tree, expected, repos)
    except composition.ProfileCompositionError:
        pass
    else:
        raise AssertionError("drifted child content was accepted through generated gitlink normalization")


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        mirror, author, first, second, tampered = (root / name for name in ("mirror.git", "author", "first", "second", "tampered"))
        git(root, "init", "--bare", "-q", str(mirror))
        git(root, "clone", "-q", str(mirror), str(author))
        git(author, "config", "user.name", "Author")
        git(author, "config", "user.email", "author@example.invalid")
        (author / "mldr.c").write_text("base\n")
        git(author, "add", "mldr.c"); git(author, "commit", "-qm", "base")
        base = git(author, "rev-parse", "HEAD")
        (author / "mldr.c").write_text("historical series\n")
        git(author, "commit", "-qam", "historical mldr series")
        source = git(author, "rev-parse", "HEAD")
        git(author, "tag", f"patch-stack/v1/bases/{base}", base)
        git(author, "tag", f"patch-stack/v1/sources/{source}", source)
        git(author, "push", "-q", "origin", "HEAD:refs/heads/main", "--tags")
        git(mirror, "symbolic-ref", "HEAD", "refs/heads/main")

        # These are intentionally identity-dependent profile checkpoints. They
        # have the same tree and are never added to the immutable mirror.
        git(author, "reset", "--hard", "-q", base)
        (author / "profile-state").write_text("homebrew verified\n")
        git(author, "add", "profile-state")
        git(author, "commit", "-qm", "generated homebrew", env=commit_env("Replay A", "a@example.invalid"))
        generated_a, generated_tree = git(author, "rev-parse", "HEAD"), git(author, "rev-parse", "HEAD^{tree}")
        generated_b = git(author, "commit-tree", generated_tree, "-p", base, "-m", "generated homebrew", env=commit_env("Replay B", "b@example.invalid"))
        git(author, "update-ref", "refs/heads/generated-b", generated_b)
        assert generated_a != generated_b
        assert git(author, "show", "-s", "--format=%T", generated_b) == generated_tree
        assert subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/patch-stack/v1/bases/{generated_a}"], cwd=mirror
        ).returncode == 1

        # The expected profile boundary is deliberately independent of either
        # generated commit ID.
        git(root, "clone", "-q", str(author), str(first))
        git(first, "config", "user.name", "Replay")
        git(first, "config", "user.email", "replay@example.invalid")
        git(first, "checkout", "-q", "--detach", generated_a)
        git(root, "clone", "-q", str(author), str(second))
        git(second, "config", "user.name", "Replay")
        git(second, "config", "user.email", "replay@example.invalid")
        git(second, "checkout", "-q", "--detach", generated_b)
        oracle = root / "oracle"
        git(root, "clone", "-q", str(author), str(oracle))
        git(oracle, "config", "user.name", "Replay")
        git(oracle, "config", "user.email", "replay@example.invalid")
        git(oracle, "checkout", "-q", "--detach", generated_a)
        git(oracle, "cherry-pick", source)
        boundary = git(oracle, "rev-parse", "HEAD^{tree}")

        lock = root / "historical.yml"
        write(lock, lock_doc(mirror, base, source, git(author, "show", "-s", "--format=%T", source)))
        entry = {"module": "darling", "patch": "darling/mldr.patch", "lock_path": str(lock)}
        composed = {"boundaries": {("darling", "darling/mldr.patch"): boundary}}
        for repo in (first, second):
            results, stats = lock_first.materialize_batch_into(repo, [entry], composition=composed)
            assert results[0]["applied_tree"] == boundary
            assert results[0]["source"] == source
            assert stats["immutable_fetch_transactions"] == 1
            assert not git(repo, "for-each-ref", "refs/west/patch-stack-lock-first")
        assert git(first, "rev-parse", "HEAD^{tree}") == git(second, "rev-parse", "HEAD^{tree}") == boundary

        git(root, "clone", "-q", str(author), str(tampered))
        git(tampered, "config", "user.name", "Replay")
        git(tampered, "config", "user.email", "replay@example.invalid")
        git(tampered, "checkout", "-q", "--detach", generated_a)
        (tampered / "mldr.c").write_text("tampered prerequisite tree\n")
        git(tampered, "commit", "-am", "tamper")
        try:
            lock_first.materialize_batch_into(tampered, [entry], composition=composed)
        except lock_first.LockFirstError:
            pass
        else:
            raise AssertionError("tampered prerequisite tree replayed")
        assert not git(tampered, "for-each-ref", "refs/west/patch-stack-lock-first")
        assert not (tampered / ".git" / "rebase-apply").exists()

        # A genuine source that is absent from its declared immutable mirror
        # remains a hard failure; generated checkpoints do not relax closure.
        missing = root / "missing.yml"
        absent = "0" * 39 + "1"
        write(missing, lock_doc(mirror, base, absent, git(author, "show", "-s", "--format=%T", source)))
        try:
            lock_first.materialize_batch_into(second, [{**entry, "lock_path": str(missing)}], composition=composed)
        except lock_first.LockFirstError:
            pass
        else:
            raise AssertionError("missing immutable source was accepted")

        # Typed prerequisite identity is locked by composition SHA, frozen
        # manifest SHA, and per-module final trees.
        frozen = root / "west.lock.yml"; frozen.write_text("manifest: frozen\n")
        mapping = root / "mapping.yml"
        mapping_payload = {"schema_version": 3, "profile": "perf", "batch_id": "perf", "expected_count": 1,
                           "composition": "perf-compose.yml", "series": [{"profile": "perf", "module": "darling", "patch": "darling/mldr.patch", "lock": "historical.yml"}]}
        write(mapping, mapping_payload)
        home_mapping = root / "home-mapping.yml"; write(home_mapping, {**mapping_payload, "profile": "homebrew", "batch_id": "home", "composition": "home-compose.yml", "series": [{"profile": "homebrew", "module": "darling", "patch": "darling/prior.patch", "lock": "historical.yml"}]})
        home = root / "home-compose.yml"
        home_payload = {"schema_version": 3, "profile": "homebrew", "prerequisites": [],
                        "frozen_manifest": {"path": "west.lock.yml", "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest()},
                        "mapping": {"path": home_mapping.name, "sha256": hashlib.sha256(home_mapping.read_bytes()).hexdigest(), "batch_id": "home", "expected_count": 1},
                        "modules": [{"module": "darling", "starting": {"tree": generated_tree}, "series": [{"patch": "darling/prior.patch", "lock": "historical.yml", "expected_applied_tree": generated_tree}], "final_tree": generated_tree, "integration_final_tree": generated_tree}]}
        write(home, home_payload)
        perf = root / "perf-compose.yml"
        perf_payload = {"schema_version": 3, "profile": "perf", "prerequisites": [{"profile": "homebrew", "composition": home.name, "sha256": hashlib.sha256(home.read_bytes()).hexdigest(), "frozen_manifest": home_payload["frozen_manifest"], "module_trees": {"darling": generated_tree}}],
                        "frozen_manifest": home_payload["frozen_manifest"],
                        "mapping": {"path": mapping.name, "sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(), "batch_id": "perf", "expected_count": 1},
                        "modules": [{"module": "darling", "starting": {"tree": generated_tree}, "series": [{"patch": "darling/mldr.patch", "lock": "historical.yml", "expected_applied_tree": boundary}], "final_tree": boundary, "integration_final_tree": boundary}]}
        write(perf, perf_payload)
        bound = composition.bind(perf, mapping_path=mapping, mapping=mapping_payload, entries=[entry])
        assert bound["prerequisites"][0]["module_trees"] == {"darling": generated_tree}
        perf_payload["prerequisites"][0]["sha256"] = "0" * 64
        write(perf, perf_payload)
        try:
            composition.bind(perf, mapping_path=mapping, mapping=mapping_payload, entries=[entry])
        except composition.ProfileCompositionError:
            pass
        else:
            raise AssertionError("wrong prerequisite composition SHA was accepted")
        assert_parent_gitlink_normalization(root)
    print("profile composition dependency contract: PASS")


if __name__ == "__main__":
    main()
