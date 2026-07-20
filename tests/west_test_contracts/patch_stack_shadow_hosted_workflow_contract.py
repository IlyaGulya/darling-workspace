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


def main() -> None:
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
            manifest.write_text(json.dumps({"workspace_commit": "c" * 40, "frozen_manifest_sha256": "d" * 64}))
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
