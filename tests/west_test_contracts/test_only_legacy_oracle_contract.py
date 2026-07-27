#!/usr/bin/env python3
"""Real disposable fixture for the isolated archive legacy oracle."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import patch_stack_legacy_oracle as oracle


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def repository(root: Path, name: str, value: str) -> tuple[Path, str, str]:
    repo = root / "sources" / name
    repo.mkdir(parents=True)
    git(repo, "init", "-q"); git(repo, "config", "user.name", "Fixture"); git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "file").write_text("base\n"); git(repo, "add", "file"); git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "file").write_text(value + "\n"); git(repo, "commit", "-am", value)
    source = git(repo, "rev-parse", "HEAD")
    return repo, base, source


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"; workspace.mkdir()
        darling, darling_base, darling_source = repository(root, "darling", "root")
        child, child_base, child_source = repository(root, "darling/src/external/child", "child")
        projects = {"darling": darling, "darling/src/external/child": child}
        profile_dir = workspace / "patches" / "homebrew"; profile_dir.mkdir(parents=True)
        rows = [
            {"module": "darling/src/external/child", "path": "child.patch", "source-commit": child_source},
            {"module": "darling", "path": "root.patch", "source-commit": darling_source},
        ]
        (profile_dir / "patches.yml").write_text(yaml.safe_dump({"integration-date": "2026-01-01T00:00:00Z", "patches": rows}, sort_keys=False))
        (workspace / "west.lock.yml").write_text(yaml.safe_dump({"manifest": {"projects": [
            {"path": "darling", "revision": darling_base},
            {"path": "darling/src/external/child", "revision": child_base},
        ]}}, sort_keys=False))
        (profile_dir / "child.patch").write_text(subprocess.run(["git", "format-patch", "--stdout", f"{child_base}..{child_source}"], cwd=child, check=True, text=True, stdout=subprocess.PIPE).stdout)
        (profile_dir / "root.patch").write_text(subprocess.run(["git", "format-patch", "--stdout", f"{darling_base}..{darling_source}"], cwd=darling, check=True, text=True, stdout=subprocess.PIPE).stdout)
        git(child, "reset", "--hard", "-q", child_base)
        git(darling, "reset", "--hard", "-q", darling_base)
        locks = workspace / "locks"; locks.mkdir()
        mapping = locks / "homebrew.yml"
        mapping.write_text(yaml.safe_dump({"schema_version": 2, "profile": "homebrew", "batch_id": "fixture", "expected_count": 2, "series": [
            {"profile": "homebrew", "module": "darling/src/external/child", "patch": "child.patch", "lock": "child.yml"},
            {"profile": "homebrew", "module": "darling", "patch": "root.patch", "lock": "root.yml"},
        ]}, sort_keys=False))
        (locks / "lock-first-profiles-v1.yml").write_text(yaml.safe_dump({"schema_version": 1, "profiles": [{"profile": "homebrew", "mapping": "homebrew.yml"}]}, sort_keys=False))
        before = {name: git(repo, "rev-parse", "HEAD") for name, repo in projects.items()}
        output = root / "oracle.json"
        old_projects = oracle.projects
        try:
            oracle.projects = lambda _workspace: projects
            oracle.apply(workspace, "homebrew", mapping, output)
        finally:
            oracle.projects = old_projects
        payload = json.loads(output.read_text())
        assert payload["mode"] == "test-only-legacy-oracle"
        assert payload["profile"] == "homebrew" and payload["batch_id"] == "fixture" and payload["expected_count"] == 2
        assert [row["module"] for row in payload["modules"]] == ["darling", "darling/src/external/child"]
        assert payload["generated_profile_lock"]["profile"] == "homebrew"
        assert payload["generated_profile_lock"]["size"] > 0
        assert payload["cleanup"] == {"root": "removed", "worktrees": "removed"}
        assert {name: git(repo, "rev-parse", "HEAD") for name, repo in projects.items()} == before
        for repo in projects.values():
            assert git(repo, "for-each-ref", "--format=%(refname)", "refs/heads/integration/") == ""
            assert "west-test-legacy-oracle-" not in git(repo, "worktree", "list", "--porcelain")
        try:
            oracle.apply(workspace, "homebrew", mapping, output)
        except oracle.OracleError as error:
            assert "already exists" in str(error)
        else:
            raise AssertionError("oracle overwrote existing evidence")
    print("test-only legacy oracle contract: PASS")


if __name__ == "__main__":
    main()
