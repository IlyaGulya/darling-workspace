#!/usr/bin/env python3
"""Contract for the canonical migration inventory's mbox-series model."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SHA = re.compile(r"^[0-9a-f]{40}$")
MBOX = re.compile(r"^From ([0-9a-f]{40}) ", re.MULTILINE)


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate mapping keys instead of last-wins."""


def _mapping(loader: UniqueKeyLoader, node: yaml.nodes.MappingNode, deep: bool = False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise AssertionError(f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def load(path: Path):
    return yaml.load(path.read_text(), Loader=UniqueKeyLoader)


def git(repo: Path, *args: str) -> str | None:
    result = subprocess.run(["git", "-C", str(repo), *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip() if result.returncode == 0 else None


def main() -> None:
    inventory = load(ROOT / "locks/patch-stack/migration-inventory-v1.yml")
    assert inventory["schema_version"] == 2
    rows = {(row["profile"], row["patch"]): row for row in inventory["stacks"]}
    assert len(rows) == len(inventory["stacks"]) == 94, "entries must be exact and unique"
    actual_summary = {name: sum(row["classification"] == name for row in rows.values()) for name in ("READY", "RECOVERABLE_LOCAL", "ALREADY_MIGRATED")}
    assert inventory["summary"] == actual_summary == {"READY": 13, "RECOVERABLE_LOCAL": 76, "ALREADY_MIGRATED": 5}
    report = (ROOT / "docs/patch-stack-canonical-migration-report.md").read_text()
    for count, name in ((13, "READY"), (76, "RECOVERABLE_LOCAL"), (5, "ALREADY_MIGRATED")):
        assert f"{count} `{name}`" in report, f"report summary missing {count} {name}"
    expected = {}
    for profile in ("arch", "homebrew", "perf"):
        metadata = load(ROOT / "patches" / profile / "patches.yml")
        for item in metadata["patches"]:
            if "module" not in item:
                continue
            artifact = ROOT / "patches" / profile / item["path"]
            commits = MBOX.findall(artifact.read_text())
            assert commits, artifact
            expected[profile, item["path"]] = (item, artifact, commits)
    assert set(rows) == set(expected), "inventory must contain exactly the frozen artifacts"
    for key, (item, artifact, commits) in expected.items():
        row = rows[key]
        assert row["artifact"] == str(artifact.relative_to(ROOT))
        assert row["artifact_count"] == 1
        assert row["ordered_commits"] == commits
        assert row["commit_count"] == len(commits)
        assert row["source_commit"] == item["source-commit"] == commits[-1]
        assert all(SHA.fullmatch(oid) for oid in [row["base_commit"], *commits, row["expected_tree"]])
        if "source-base" in item:
            assert row["base_commit"] == item["source-base"]
        repo = ROOT.parent / ("darling" if item["module"] == "darling" else item["module"])
        assert row["linearity"] == "verified", key
        if git(repo, "cat-file", "-e", f"{commits[-1]}^{{commit}}") is None:
            # Linearity is frozen audit evidence. A checkout that lacks these
            # objects cannot disprove it; it merely skips this local recheck.
            continue
        parents = [git(repo, "show", "-s", "--format=%P", oid) for oid in commits]
        assert parents[0] and parents[0].split() == [row["base_commit"]], key
        assert all(parents[i] and parents[i].split() == [commits[i - 1]] for i in range(1, len(commits))), key
    closures = {
        "ALREADY_MIGRATED": "hosted_immutable_clean_odb",
        "READY": "frozen_bundle_clean_odb",
        "RECOVERABLE_LOCAL": "trusted_worktree_only",
    }
    for row in rows.values():
        assert row["object_closure"] == closures[row["classification"]], row["patch"]
        if row["classification"] != "ALREADY_MIGRATED":
            assert "lock" not in row
            continue
        lock_path = ROOT / row["lock"]
        lock = load(lock_path)
        assert lock["upstream"]["url"] == row["upstream"]
        assert lock["mirror"]["url"] == row["downstream"]
        assert lock["upstream"]["base_commit"] == row["base_commit"]
        assert lock["ordered_commits"] == row["ordered_commits"]
        assert lock["source_commit"] == row["source_commit"]
        assert lock["expected_tree"] == row["expected_tree"]
    host_tier = (ROOT / "ci/run-test-tier.sh").read_text().split("\thost)\n", 1)[1].split("\tguest-smoke)", 1)[0]
    runner = "tests/run-patch-stack-migration-inventory-contract.sh"
    assert runner in host_tier
    assert host_tier.index(runner) < host_tier.index("exec west test"), "inventory runner must precede west test"
    print(f"migration inventory contract: PASS ({len(rows)} series, {sum(r['commit_count'] for r in rows.values())} commits)")


if __name__ == "__main__":
    main()
