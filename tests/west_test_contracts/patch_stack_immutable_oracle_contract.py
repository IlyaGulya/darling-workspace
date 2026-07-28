#!/usr/bin/env python3
"""Independent clean-ODB immutable-ref oracle contract."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))
import patch_stack_immutable_oracle as oracle


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def commit(repo: Path, message: str) -> str:
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def immutable_lock(
    mirror: Path,
    project: str,
    base: str,
    source: str,
    expected_tree: str,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "project": {"name": project, "path": "."},
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
        "expected_tree": expected_tree,
    }


with tempfile.TemporaryDirectory(prefix="immutable-oracle-contract-") as temp:
    root = Path(temp)
    source_root = root / "sources"
    child = source_root / "child"
    child.mkdir(parents=True)
    git(child, "init", "-q")
    git(child, "config", "user.name", "Oracle Fixture")
    git(child, "config", "user.email", "oracle-fixture@example.invalid")
    (child / "child.txt").write_text("base\n")
    git(child, "add", "child.txt")
    child_base = commit(child, "child base")
    child_base_tree = git(child, "rev-parse", "HEAD^{tree}")
    (child / "child.txt").write_text("canonical child\n")
    git(child, "add", "child.txt")
    child_source = commit(child, "child canonical")
    child_tree = git(child, "rev-parse", "HEAD^{tree}")

    parent = source_root / "darling"
    parent.mkdir(parents=True)
    git(parent, "init", "-q")
    git(parent, "config", "user.name", "Oracle Fixture")
    git(parent, "config", "user.email", "oracle-fixture@example.invalid")
    (parent / "root.txt").write_text("base\n")
    git(parent, "add", "root.txt")
    git(
        parent,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{child_base},src/external/child",
    )
    parent_base = commit(parent, "parent base")
    parent_base_tree = git(parent, "rev-parse", "HEAD^{tree}")
    (parent / "root.txt").write_text("canonical parent\n")
    git(parent, "add", "root.txt")
    parent_source = commit(parent, "parent canonical")
    parent_tree = git(parent, "rev-parse", "HEAD^{tree}")
    nested = parent / "src/external/child"
    nested.mkdir(parents=True)
    git(nested, "init", "-q")
    git(nested, "fetch", "-q", str(child), child_source)
    git(nested, "checkout", "-q", "--detach", "FETCH_HEAD")
    git(parent, "add", "src/external/child")
    parent_integration_tree = git(parent, "write-tree")
    git(parent, "reset", "-q", "HEAD")

    child_mirror = root / "child.git"
    parent_mirror = root / "parent.git"
    git(root, "clone", "--bare", "-q", str(child), str(child_mirror))
    git(root, "clone", "--bare", "-q", str(parent), str(parent_mirror))
    for mirror, base, source in (
        (child_mirror, child_base, child_source),
        (parent_mirror, parent_base, parent_source),
    ):
        git(
            mirror,
            "update-ref",
            f"refs/tags/patch-stack/v1/bases/{base}",
            base,
        )
        git(
            mirror,
            "update-ref",
            f"refs/tags/patch-stack/v1/sources/{source}",
            source,
        )

    workspace = root / "workspace"
    locks = workspace / "locks" / "patch-stack"
    profile = workspace / "patches" / "homebrew"
    locks.mkdir(parents=True)
    profile.mkdir(parents=True)
    patches = [
        {
            "module": "darling/src/external/child",
            "path": "child/canonical.patch",
        },
        {"module": "darling", "path": "darling/canonical.patch"},
    ]
    (profile / "patches.yml").write_text(
        yaml.safe_dump(
            {
                "integration-date": "2026-01-01T00:00:00+00:00",
                "patches": patches,
            },
            sort_keys=False,
        )
    )
    frozen = workspace / "west.lock.yml"
    frozen.write_text(
        yaml.safe_dump(
            {
                "manifest": {
                    "projects": [
                        {
                            "name": "darling",
                            "path": "darling",
                            "revision": parent_base,
                        },
                        {
                            "name": "child",
                            "path": "darling/src/external/child",
                            "revision": child_base,
                        },
                    ]
                }
            },
            sort_keys=False,
        )
    )
    (locks / "child.yml").write_text(
        yaml.safe_dump(
            immutable_lock(
                child_mirror,
                "child",
                child_base,
                child_source,
                child_tree,
            ),
            sort_keys=False,
        )
    )
    (locks / "parent.yml").write_text(
        yaml.safe_dump(
            immutable_lock(
                parent_mirror,
                "darling",
                parent_base,
                parent_source,
                parent_tree,
            ),
            sort_keys=False,
        )
    )
    mapping = locks / "fixture-series-v2.yml"
    mapping_value = {
        "schema_version": 3,
        "profile": "homebrew",
        "batch_id": "immutable-oracle-fixture",
        "expected_count": 2,
        "composition": "fixture-composition-v2.yml",
        "series": [
            {
                "profile": "homebrew",
                "module": patches[0]["module"],
                "patch": patches[0]["path"],
                "lock": "child.yml",
            },
            {
                "profile": "homebrew",
                "module": patches[1]["module"],
                "patch": patches[1]["path"],
                "lock": "parent.yml",
            },
        ],
    }
    mapping.write_text(yaml.safe_dump(mapping_value, sort_keys=False))
    composition = {
        "schema_version": 3,
        "profile": "homebrew",
        "prerequisites": [],
        "frozen_manifest": {
            "path": "west.lock.yml",
            "sha256": hashlib.sha256(frozen.read_bytes()).hexdigest(),
        },
        "mapping": {
            "path": mapping.name,
            "sha256": hashlib.sha256(mapping.read_bytes()).hexdigest(),
            "batch_id": mapping_value["batch_id"],
            "expected_count": 2,
        },
        "modules": [
            {
                "module": patches[0]["module"],
                "starting": {"tree": child_base_tree},
                "series": [
                    {
                        "patch": patches[0]["path"],
                        "lock": "child.yml",
                        "expected_applied_tree": child_tree,
                    }
                ],
                "final_tree": child_tree,
                "integration_final_tree": child_tree,
            },
            {
                "module": "darling",
                "starting": {"tree": parent_base_tree},
                "series": [
                    {
                        "patch": patches[1]["path"],
                        "lock": "parent.yml",
                        "expected_applied_tree": parent_tree,
                    }
                ],
                "final_tree": parent_tree,
                "integration_final_tree": parent_integration_tree,
            },
        ],
    }
    (locks / "fixture-composition-v2.yml").write_text(
        yaml.safe_dump(composition, sort_keys=False)
    )

    baseline = {
        "child": (
            git(child, "rev-parse", "HEAD"),
            git(child, "status", "--porcelain=v1"),
        ),
        "parent": (
            git(parent, "rev-parse", "HEAD"),
            git(parent, "status", "--porcelain=v1"),
        ),
    }
    output = root / "oracle.json"
    oracle.apply(workspace, "homebrew", mapping, output)
    payload = json.loads(output.read_text())
    assert payload["oracle_schema_version"] == 2
    assert payload["mode"] == "immutable-cherry-pick-oracle"
    assert payload["profile_order"] == ["homebrew"]
    assert payload["batches"][0]["batch_id"] == "immutable-oracle-fixture"
    assert payload["batches"][0]["expected_count"] == 2
    assert payload["batches"][0]["series_order"] == [
        {"module": patch["module"], "patch": patch["path"]}
        for patch in patches
    ]
    assert [entry["verdict"] for entry in payload["batches"][0]["series"]] == [
        "VALID",
        "VALID",
    ]
    module_trees = {
        row["module"]: row["tree"] for row in payload["modules"]
    }
    assert set(module_trees) == {
        "darling",
        "darling/src/external/child",
    }
    assert len(module_trees["darling"]) == 40
    assert module_trees["darling"] != parent_tree
    assert module_trees["darling/src/external/child"] == child_tree
    assert payload["clean_odb"] == {
        "module_count": 2,
        "immutable_fetch_transactions": 2,
        "alternates": 0,
        "shallow": 0,
        "partial": 0,
    }
    assert payload["cleanup"] == {
        "root": "removed",
        "worktrees": "removed",
        "refs": "removed",
    }
    assert len(payload["generated_profile_locks"]) == 1
    assert payload["generated_profile_locks"][0]["profile"] == "homebrew"
    assert baseline == {
        "child": (
            git(child, "rev-parse", "HEAD"),
            git(child, "status", "--porcelain=v1"),
        ),
        "parent": (
            git(parent, "rev-parse", "HEAD"),
            git(parent, "status", "--porcelain=v1"),
        ),
    }
    assert not list(Path(tempfile.gettempdir()).glob("west-immutable-oracle-*"))

    try:
        oracle.apply(workspace, "homebrew", mapping, output)
    except oracle.OracleError as error:
        assert "already exists" in str(error)
    else:
        raise AssertionError("immutable oracle overwrote existing evidence")

    git(
        child_mirror,
        "update-ref",
        f"refs/tags/patch-stack/v1/sources/{child_source}",
        child_base,
    )
    rejected = root / "rejected.json"
    try:
        oracle.apply(workspace, "homebrew", mapping, rejected)
    except oracle.OracleError as error:
        assert "source ref moved" in str(error), error
    else:
        raise AssertionError("immutable oracle accepted a moved immutable ref")
    assert not rejected.exists()
    assert not list(Path(tempfile.gettempdir()).glob("west-immutable-oracle-*"))

source = (ROOT / "tests/patch_stack_immutable_oracle.py").read_text()
assert "cherry-pick" in source
assert "materialize_batch_into" not in source
assert "format-patch" not in source
assert "for module in sorted(entries_by_module)" in source
assert "target_plan.batch[\"module_order\"]" not in source
assert "read_bytes()" not in source.split("def load_profile", 1)[1].split(
    "def profile_stack", 1
)[0]
print("patch-stack immutable oracle contract: PASS")
