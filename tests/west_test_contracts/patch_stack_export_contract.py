#!/usr/bin/env python3
"""Clean-ODB canonical review/recovery export contract."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_export
from patch_stack_lock_first import LockFirstPlan


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def must_fail(callback, message: str) -> None:
    try:
        callback()
    except patch_stack_export.ExportError as error:
        assert message in str(error), error
    else:
        raise AssertionError("canonical exporter accepted invalid input")


def tree(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


with tempfile.TemporaryDirectory(prefix="patch-stack-export-contract-") as temp:
    root = Path(temp)
    source = root / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Export Contract")
    git(source, "config", "user.email", "export-contract@example.invalid")
    (source / "fixture").write_text("base\n")
    git(source, "add", "fixture")
    git(source, "commit", "-qm", "base")
    base = git(source, "rev-parse", "HEAD")
    commits: list[str] = []
    trees: list[str] = []
    for index in (1, 2):
        (source / "fixture").write_text(f"change {index}\n")
        git(source, "commit", "-qam", f"change {index}")
        commits.append(git(source, "rev-parse", "HEAD"))
        trees.append(git(source, "rev-parse", "HEAD^{tree}"))

    mirror = root / "immutable.git"
    git(root, "clone", "--bare", "-q", str(source), str(mirror))
    boundaries = [base, commits[0]]
    for oid in {base, *commits}:
        git(mirror, "update-ref", f"refs/tags/patch-stack/v1/bases/{oid}", oid)
        git(mirror, "update-ref", f"refs/tags/patch-stack/v1/sources/{oid}", oid)

    entries = []
    for index, (boundary, commit, expected_tree) in enumerate(
        zip(boundaries, commits, trees, strict=True), 1
    ):
        lock_path = root / f"series-{index}.yml"
        lock_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 2,
                    "project": {"name": "fixture", "path": "."},
                    "upstream": {
                        "url": mirror.as_uri(),
                        "base_commit": boundary,
                    },
                    "mirror": {
                        "url": mirror.as_uri(),
                        "base_ref": (
                            f"refs/tags/patch-stack/v1/bases/{boundary}"
                        ),
                        "base_oid": boundary,
                        "source_ref": (
                            f"refs/tags/patch-stack/v1/sources/{commit}"
                        ),
                        "source_oid": commit,
                    },
                    "source_commit": commit,
                    "ordered_commits": [commit],
                    "expected_tree": expected_tree,
                },
                sort_keys=False,
            )
        )
        entries.append(
            {
                "profile": "fixture",
                "module": "vendor/fixture",
                "patch": f"review/series-{index}.patch",
                "lock": lock_path.name,
                "lock_path": str(lock_path),
            }
        )
    order = [
        {"module": entry["module"], "patch": entry["patch"]}
        for entry in entries
    ]
    plan = LockFirstPlan(
        entries,
        {
            "batch_id": "fixture-export",
            "expected_count": 2,
            "module_order": ["vendor/fixture"],
            "series_order": order,
        },
    )

    first, second = root / "first", root / "second"
    first_evidence = patch_stack_export.export_profile("fixture", plan, first)
    second_evidence = patch_stack_export.export_profile("fixture", plan, second)
    assert first_evidence == second_evidence
    assert tree(first) == tree(second)
    assert first_evidence["mode"] == "immutable-lock-format-patch"
    assert first_evidence["expected_count"] == 2
    assert first_evidence["series_order"] == order
    assert first_evidence["clean_odb"] == {
        "module_count": 1,
        "immutable_fetch_transactions": 1,
        "alternates": 0,
        "shallow": 0,
        "partial": 0,
    }
    for index, row in enumerate(first_evidence["series"]):
        mbox = first / row["mbox"]
        assert mbox.is_file() and not mbox.is_symlink()
        assert row["commit_count"] == 1
        assert row["ordered_commits"] == [commits[index]]
        assert row["resulting_tree"] == trees[index]
        assert row["sha256"] == hashlib.sha256(mbox.read_bytes()).hexdigest()
        assert len(row["stable_patch_ids"]) == 1
    assert not any(path.name == ".git" for path in first.rglob("*"))
    assert not any(
        path.suffix in {".bundle", ".pack", ".idx"}
        for path in first.rglob("*")
    )

    must_fail(
        lambda: patch_stack_export.export_profile("fixture", plan, first),
        "already exists",
    )
    target = root / "target"
    target.mkdir()
    linked = root / "linked"
    linked.symlink_to(target, target_is_directory=True)
    must_fail(
        lambda: patch_stack_export.export_profile("fixture", plan, linked),
        "already exists",
    )

    malformed = LockFirstPlan(
        [entries[0], entries[0]],
        {
            "batch_id": "invalid",
            "expected_count": 2,
            "module_order": ["vendor/fixture"],
            "series_order": [order[0], order[0]],
        },
    )
    rejected = root / "rejected"
    must_fail(
        lambda: patch_stack_export.export_profile(
            "fixture", malformed, rejected
        ),
        "order/count/identity",
    )
    assert not rejected.exists()

    bad_lock = yaml.safe_load(Path(entries[0]["lock_path"]).read_text())
    bad_lock["mirror"]["source_oid"] = base
    bad_path = root / "bad.yml"
    bad_path.write_text(yaml.safe_dump(bad_lock, sort_keys=False))
    bad_entry = dict(entries[0], lock="bad.yml", lock_path=str(bad_path))
    bad_plan = LockFirstPlan(
        [bad_entry],
        {
            "batch_id": "bad",
            "expected_count": 1,
            "module_order": ["vendor/fixture"],
            "series_order": [order[0]],
        },
    )
    failed = root / "failed"
    must_fail(
        lambda: patch_stack_export.export_profile("fixture", bad_plan, failed),
        "source",
    )
    assert not failed.exists()
    assert not list(root.glob("west-patch-lock-export-*"))

source_text = (ROOT / "west_commands/patch_stack_export.py").read_text()
assert "format-patch" in source_text
assert "validate_fetched_lock" in source_text
assert "patches/" not in source_text
print("patch-stack canonical export contract: PASS")
