#!/usr/bin/env python3
"""Contracts for the manual-only hosted Phase 3B acceptance workflow."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ci"))
import patch_stack_shadow_acceptance as acceptance


def must_fail(function, *args) -> None:
    try:
        function(*args)
    except acceptance.AcceptanceError:
        return
    raise AssertionError("acceptance unexpectedly passed")


def run(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def parent_clean_fixtures(root: Path) -> None:
    """Exercise parent_clean() against real Git status and repositories."""
    parent, source = root / "parent", root / "source"
    run("git", "init", "-q", str(source)); run("git", "-C", str(source), "config", "user.name", "Test"); run("git", "-C", str(source), "config", "user.email", "test@example.invalid")
    (source / "x").write_text("x\n"); run("git", "-C", str(source), "add", "x"); run("git", "-C", str(source), "commit", "-qm", "base")
    run("git", "init", "-q", str(parent)); run("git", "-C", str(parent), "config", "user.name", "Test"); run("git", "-C", str(parent), "config", "user.email", "test@example.invalid")
    run("git", "-C", str(parent), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), "child"); run("git", "-C", str(parent), "commit", "-qm", "submodule")
    child = parent / "child"; (child / "x").write_text("changed\n"); run("git", "-C", str(child), "commit", "-am", "advance")
    nested = {"child": {"path": child}}
    assert acceptance.parent_clean(parent, nested) == [{"xy": " M", "path": "child", "kind": "modified_gitlink"}]
    run("git", "-C", str(parent), "add", "child"); must_fail(acceptance.parent_clean, parent, nested)
    run("git", "-C", str(parent), "reset", "--hard", "-q", "HEAD"); run("git", "-C", str(child), "reset", "--hard", "-q", "HEAD~1")
    untracked = parent / "nested"; run("git", "init", "-q", str(untracked)); run("git", "-C", str(untracked), "config", "user.name", "Test"); run("git", "-C", str(untracked), "config", "user.email", "test@example.invalid"); (untracked / "x").write_text("x\n"); run("git", "-C", str(untracked), "add", "x"); run("git", "-C", str(untracked), "commit", "-qm", "nested")
    assert acceptance.parent_clean(parent, {"nested": {"path": untracked}}) == [{"xy": "??", "path": "nested/", "kind": "untracked_nested_repo"}]
    (parent / "extra").write_text("x"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}})
    (parent / "extra").unlink(); (untracked / "x").write_text("dirty"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}})
    (untracked / "x").write_text("x\n"); run("git", "-C", str(untracked), "checkout", "--", "x")
    # Exact path and exact child top-level are required: a managed-looking
    # directory and a repository rooted elsewhere are not enough.
    must_fail(acceptance.parent_clean, parent, {"nested": {"path": root}})
    (parent / "nested").rename(parent / "other"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": parent / "nested"}})
    (parent / "other").rename(parent / "nested"); (parent / "nested").rename(parent / "target"); (parent / "nested").symlink_to("target", target_is_directory=True); must_fail(acceptance.parent_clean, parent, {"nested": {"path": parent / "nested"}})
    (parent / "nested").unlink(); (parent / "target").rename(parent / "nested")
    # An unmanaged directory/file, and an untracked nested path with a wrong
    # Git top-level, all fail before any status normalization.
    (parent / "unmanaged").mkdir(); (parent / "unmanaged/x").write_text("x\n"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}}); (parent / "unmanaged/x").unlink(); (parent / "unmanaged").rmdir()
    (parent / "file").write_text("x\n"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}}); (parent / "file").unlink()
    (parent / "plain").write_text("plain\n"); run("git", "-C", str(parent), "add", "plain"); run("git", "-C", str(parent), "commit", "-qm", "plain")
    (parent / "plain").unlink(); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}}); run("git", "-C", str(parent), "checkout", "--", "plain")
    (parent / "plain").rename(parent / "renamed"); run("git", "-C", str(parent), "add", "-A"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}})
    run("git", "-C", str(parent), "reset", "--hard", "-q", "HEAD"); (parent / "copy").write_text((parent / "plain").read_text()); run("git", "-C", str(parent), "add", "copy"); must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}})
    run("git", "-C", str(parent), "reset", "--hard", "-q", "HEAD")
    wrong_top = parent / "wrong-top"; wrong_top.mkdir(); (wrong_top / "child").mkdir(); (wrong_top / "child/x").write_text("x\n")
    must_fail(acceptance.parent_clean, parent, {"wrong-top": {"path": wrong_top / "child"}})
    original_raw = acceptance.git_raw
    acceptance.git_raw = lambda *_args: (_ for _ in ()).throw(acceptance.AcceptanceError("forced git failure"))
    try:
        must_fail(acceptance.parent_clean, parent, {"nested": {"path": untracked}})
    finally:
        acceptance.git_raw = original_raw


def capture_git_failure_fixture(root: Path) -> None:
    """A failed `git show HEAD:west.lock.yml` is an AcceptanceError."""
    workspace = root / "capture"; (workspace / "patches/homebrew").mkdir(parents=True)
    (workspace / "patches/homebrew/patches.yml").write_text("patches: []\n")
    (workspace / "patches/homebrew/west.lock.yml").write_text("projects: {}\n")
    (workspace / "west.lock.yml").write_text("manifest: {}\n")
    original_command, original_raw = acceptance.command, acceptance.git_raw
    acceptance.command = lambda _workspace, *args: str(workspace) if args == ("west", "topdir") else "darling\tdarling"
    def show_fails(_repo: Path, *args: str) -> bytes:
        if args == ("status", "--porcelain=v1", "-z", "--untracked-files=all"):
            return b" M patches/homebrew/west.lock.yml\0"
        if args == ("show", "HEAD:west.lock.yml"):
            raise acceptance.AcceptanceError("forced git show failure")
        raise AssertionError(f"unexpected git_raw call: {args}")
    acceptance.git_raw = show_fails
    try:
        must_fail(acceptance.capture, workspace, "homebrew", root / "modules.json", root / "manifest.json")
    finally:
        acceptance.command, acceptance.git_raw = original_command, original_raw


def main() -> None:
    generated = "patches/homebrew/west.lock.yml"
    assert acceptance.allowed_generated_status(acceptance.parse_porcelain(f" M {generated}\0".encode()), "homebrew")
    assert not acceptance.allowed_generated_status(acceptance.parse_porcelain(f"M  {generated}\0".encode()), "homebrew")
    for raw in (b" M patches/homebrew/west.lock.yml\0 M extra\0", b"?? patches/homebrew/west.lock.yml\0", b" D patches/homebrew/west.lock.yml\0", b"R  old\0new\0", b"C  old\0new\0", b"broken\0"):
        try:
            assert not acceptance.allowed_generated_status(acceptance.parse_porcelain(raw), "homebrew")
        except acceptance.AcceptanceError:
            pass
    workflow = (ROOT / ".github/workflows/patch-stack-shadow.yml").read_text()
    assert "on:\n  workflow_dispatch:" in workflow
    assert "push:" not in workflow and "schedule:" not in workflow
    assert "runs-on: ubuntu-latest" in workflow and "timeout-minutes: 25" in workflow
    assert "fetch-depth: 0" in workflow and "--no-local --no-hardlinks" in workflow
    assert "actions/cache@" not in workflow and "XDG_CACHE_HOME:" in workflow
    assert "TMPDIR: ${{ runner.temp }}" not in workflow
    assert "SHADOW_ROOT: ${{ runner.temp }}" not in workflow
    assert workflow.index("- name: Configure disposable paths") < workflow.index("- uses: actions/checkout@v7")
    assert "printf 'SHADOW_ROOT=%s\\n' \"$RUNNER_TEMP/patch-stack-shadow\" >>\"$GITHUB_ENV\"" in workflow
    assert "printf 'SHADOW_TOOLS=%s\\n' \"$RUNNER_TEMP/patch-stack-shadow-tools\" >>\"$GITHUB_ENV\"" in workflow
    assert "printf 'TMPDIR=%s\\n' \"$RUNNER_TEMP\" >>\"$GITHUB_ENV\"" in workflow
    assert "python3 -m venv \"$SHADOW_TOOLS\"" in workflow
    assert "\"$SHADOW_TOOLS/bin/pip\" install west" in workflow
    assert "\"$SHADOW_TOOLS/bin\" >>\"$GITHUB_PATH\"" in workflow
    assert workflow.count("HOME=\"$home\" west --version") == 1
    assert workflow.count("HOME=\"$home\" \"$SHADOW_TOOLS/bin/python\" -c 'import west'") == 1
    assert workflow.count("git -C \"$project\" config user.name 'West Shadow Acceptance'") == 2
    assert workflow.count("git -C \"$project\" config user.email west-shadow@example.invalid") == 2
    assert workflow.count("git -C \"$project\" config --local user.name") == 2
    assert workflow.count("git -C \"$project\" config --local user.email") == 2
    assert workflow.count('SHADOW_ROOT="${SHADOW_ROOT:-$RUNNER_TEMP/patch-stack-shadow}"') >= 3
    assert 'mkdir -p "$SHADOW_ROOT/evidence"' in workflow
    assert "\\( -name .git -type d -o -name .git -type f \\)" in workflow
    assert "west-patch-shadow-* west-lock-materialize-*" in workflow
    assert 'SHADOW_TOOLS="${SHADOW_TOOLS:-$RUNNER_TEMP/patch-stack-shadow-tools}"' in workflow
    assert 'rm -rf -- "$SHADOW_ROOT/control" "$SHADOW_ROOT/shadow" "$SHADOW_TOOLS"' in workflow
    assert '[[ ! -e "$SHADOW_TOOLS" ]] || status=1' in workflow
    assert 'git -C "$worktree" rev-parse --git-path rebase-apply' in workflow
    assert 'git -C "$worktree" am --abort' in workflow
    assert workflow.count("west patch apply --profile homebrew\n") == 1
    assert workflow.count("west patch apply --profile homebrew --shadow-lock") == 1
    assert "--shadow-evidence \"$SHADOW_ROOT/evidence/shadow-evidence.json\"" in workflow
    assert workflow.count("if: always()") >= 4
    assert "actions/upload-artifact@v7" in workflow
    assert "assert-no-live-west-test" in workflow
    assert "west-patch-shadow-*" in workflow

    lock = yaml.safe_load((ROOT / "locks/patch-stack/darling-sandbox-exec-pass-through-v1.yml").read_text())
    row = {"module": "darling", "path": "darling", "integration_oid": "a" * 40, "tree": "b" * 40, "status": ""}
    evidence = {
        "verdict": "VALID", "fetched_legacy_base_oid": lock["upstream"]["base_commit"],
        "source_oid": lock["source_commit"], "legacy_mbox_ordered_commits": lock["ordered_commits"],
        "legacy_mbox_commit_count": len(lock["ordered_commits"]),
        "legacy_resulting_tree": lock["expected_tree"], "canonical_resulting_tree": lock["expected_tree"],
        "cleanup": {"root": "removed"},
    }
    original_transactions = acceptance.assert_no_transaction_state
    original_command = acceptance.command
    acceptance.assert_no_transaction_state = lambda _workspace: None
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent_clean_fixtures(root / "parent-clean")
            capture_git_failure_fixture(root / "capture-git-failure")
            # West project names are normalized independently from their
            # manifest paths.  patches.yml must select by the latter.
            acceptance.command = lambda _workspace, *args: str(root) if args == ("west", "topdir") else "darling\tdarling\ndarling-src-external-xnu\tdarling/src/external/xnu"
            indexed = acceptance.projects(root)
            selected = acceptance.touched_projects({"patches": [{"module": "darling/src/external/xnu"}]}, indexed)
            assert selected == {"darling", "darling/src/external/xnu"}
            assert indexed["darling/src/external/xnu"]["name"] == "darling-src-external-xnu"
            acceptance.command = lambda _workspace, *args: str(root) if args == ("west", "topdir") else "a\tdarling\nb\tdarling"
            must_fail(acceptance.projects, root)
            acceptance.command = original_command
            control = root / "control.json"; shadow = root / "shadow.json"
            manifest = root / "manifest.json"; other_manifest = root / "other-manifest.json"
            value = {"profile": "homebrew", "modules": [row]}
            control.write_text(json.dumps(value)); shadow.write_text(json.dumps(value))
            manifest.write_text(json.dumps({"workspace_commit": "c" * 40, "frozen_manifest_sha256": "d" * 64, "generated_profile_lock": {"sha256": "e" * 64, "size": 12}}))
            other_manifest.write_text(manifest.read_text())
            evidence_path = root / "shadow-evidence.json"; evidence_path.write_text(json.dumps(evidence))
            result = root / "result.json"
            args = (control, shadow, manifest, other_manifest, evidence_path, ROOT / "locks/patch-stack/darling-sandbox-exec-pass-through-v1.yml", root, root, result)
            acceptance.compare(*args)
            assert json.loads(result.read_text())["verdict"] == "VALID"
            must_fail(acceptance.compare, control, shadow, manifest, other_manifest, root / "missing.json", args[5], root, root, result)
            duplicate = root / "shadow-evidence-extra.json"; duplicate.write_text("{}")
            must_fail(acceptance.compare, *args); duplicate.unlink()
            bad = dict(value); bad["modules"] = [dict(row, tree="0" * 40)]
            shadow.write_text(json.dumps(bad)); must_fail(acceptance.compare, *args); shadow.write_text(json.dumps(value))
            bad["modules"] = [dict(row, integration_oid="0" * 40)]
            shadow.write_text(json.dumps(bad)); must_fail(acceptance.compare, *args); shadow.write_text(json.dumps(value))
            bad["modules"] = []
            shadow.write_text(json.dumps(bad)); must_fail(acceptance.compare, *args); shadow.write_text(json.dumps(value))
            bad["modules"] = [row, dict(row, module="extra", path="extra")]
            shadow.write_text(json.dumps(bad)); must_fail(acceptance.compare, *args); shadow.write_text(json.dumps(value))
            bad["modules"] = [dict(row, status=" M dirty")]
            control.write_text(json.dumps(bad)); shadow.write_text(json.dumps(bad)); must_fail(acceptance.compare, *args)
            control.write_text(json.dumps(value)); shadow.write_text(json.dumps(value))
            bad_evidence = dict(evidence, cleanup={"root": "failed"}); evidence_path.write_text(json.dumps(bad_evidence)); must_fail(acceptance.compare, *args)
            evidence_path.write_text(json.dumps(evidence))
            source, artifact = root / "source", root / "artifact"; source.mkdir(); artifact.mkdir()
            (source / "cleanup.txt").write_text("ok\n"); acceptance.stage(source, artifact)
            (artifact / "objects").mkdir(); must_fail(acceptance.stage, source, artifact)

            # Both temporary root namespaces are cleanup blockers.  These are
            # intentionally real directories, not string-only assertions.
            for name in ("west-patch-shadow-negative", "west-lock-materialize-negative"):
                disposable = root / name; disposable.mkdir()
                assert list(root.glob("west-patch-shadow-*") if name.startswith("west-patch-shadow") else root.glob("west-lock-materialize-*")) == [disposable]

            # Check the real materializer/result namespaces against a genuine
            # Git object database rather than only comparing strings.
            repo = root / "repo"; subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
            (repo / "x").write_text("x\n"); subprocess.run(["git", "-C", str(repo), "add", "x"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
            original_projects = acceptance.projects
            acceptance.projects = lambda _workspace: {"darling": {"name": "darling", "path": repo}}
            try:
                acceptance.assert_no_transaction_state = original_transactions
                acceptance.assert_no_transaction_state(root)
                subprocess.run(["git", "-C", str(repo), "update-ref", "refs/west/patch-stack-materialize/test", "HEAD"], check=True)
                must_fail(acceptance.assert_no_transaction_state, root)
                subprocess.run(["git", "-C", str(repo), "update-ref", "-d", "refs/west/patch-stack-materialize/test"], check=True)
                subprocess.run(["git", "-C", str(repo), "update-ref", "refs/west/patch-stack-results/test", "HEAD"], check=True)
                must_fail(acceptance.assert_no_transaction_state, root)
            finally:
                acceptance.projects = original_projects
                acceptance.assert_no_transaction_state = lambda _workspace: None
    finally:
        acceptance.assert_no_transaction_state = original_transactions
        acceptance.command = original_command
    print("patch-stack shadow hosted workflow contract: PASS")


if __name__ == "__main__":
    main()
