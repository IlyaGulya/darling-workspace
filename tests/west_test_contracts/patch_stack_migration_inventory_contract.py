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
    assert len(rows) == len(inventory["stacks"]) == 100, "entries must be exact and unique"
    classifications = (
        "READY",
        "RECOVERABLE_LOCAL",
        "ALREADY_MIGRATED",
        "PUBLICATION_PENDING",
    )
    actual_summary = {
        name: sum(row["classification"] == name for row in rows.values())
        for name in classifications
    }
    assert inventory["summary"] == actual_summary == {
        "READY": 0,
        "RECOVERABLE_LOCAL": 0,
        "ALREADY_MIGRATED": 94,
        "PUBLICATION_PENDING": 6,
    }
    report = (ROOT / "docs/patch-stack-canonical-migration-report.md").read_text()
    for count, name in (
        (0, "READY"),
        (0, "RECOVERABLE_LOCAL"),
        (94, "ALREADY_MIGRATED"),
        (6, "PUBLICATION_PENDING"),
    ):
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
    # The canonical Homebrew mapping is a direct archive-series inventory.
    # Perf and Arch mappings contain reviewed profile-integration restacks, so
    # their boundary locks intentionally differ from the archive provenance
    # rows tracked here.
    mapping_files = {"homebrew": "lock-first-series-v2.yml"}
    mapped = {}
    for profile, filename in mapping_files.items():
        mapping = load(ROOT / "locks/patch-stack" / filename)
        assert mapping["schema_version"] == 3
        assert mapping["profile"] == profile
        assert mapping["expected_count"] == len(mapping["series"])
        for entry in mapping["series"]:
            key = (profile, entry["patch"])
            assert key not in mapped, f"duplicate typed mapping entry: {key}"
            mapped[key] = entry
    homebrew_rows = {key for key in rows if key[0] == "homebrew"}
    assert set(mapped) == homebrew_rows, (
        "typed Homebrew mapping and migration inventory must describe the same exact series"
    )
    for key, (item, artifact, commits) in expected.items():
        row = rows[key]
        if key in mapped:
            mapped_entry = mapped[key]
            assert mapped_entry["module"] == item["module"], (
                key,
                "typed mapping module differs from patch metadata",
            )
            mapped_lock = f"locks/patch-stack/{mapped_entry['lock']}"
            assert row.get("lock") == mapped_lock, (
                key,
                "typed mapping lock differs from canonical migration inventory",
                mapped_lock,
                row.get("lock"),
            )
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
        "PUBLICATION_PENDING": "local_append_only_clean_odb",
    }
    canonical_fields = {
        "canonical_ordered_commits",
        "canonical_commit_count",
        "canonical_source_commit",
        "canonical_expected_tree",
    }
    for row in rows.values():
        assert row["object_closure"] == closures[row["classification"]], row["patch"]
        present_canonical_fields = canonical_fields.intersection(row)
        assert present_canonical_fields in (set(), canonical_fields), (
            row["patch"],
            "canonical lock override fields must be supplied as one typed set",
            sorted(present_canonical_fields),
        )
        canonical_commits = row.get("canonical_ordered_commits", row["ordered_commits"])
        canonical_count = row.get("canonical_commit_count", row["commit_count"])
        canonical_source = row.get("canonical_source_commit", row["source_commit"])
        canonical_tree = row.get("canonical_expected_tree", row["expected_tree"])
        canonical_base = row.get("canonical_base_commit", row["base_commit"])
        assert canonical_commits and canonical_count == len(canonical_commits), row["patch"]
        assert canonical_source == canonical_commits[-1], row["patch"]
        assert all(
            SHA.fullmatch(oid)
            for oid in [canonical_base, *canonical_commits, canonical_tree]
        ), row["patch"]
        if "canonical_base_commit" in row:
            assert canonical_fields.issubset(row), (
                row["patch"],
                "canonical base override requires the complete canonical lock tuple",
            )
        key = (row["profile"], row["patch"])
        if key in mapped:
            mapped_lock = load(ROOT / "locks/patch-stack" / mapped[key]["lock"])
            assert mapped_lock["ordered_commits"] == canonical_commits, row["patch"]
            assert mapped_lock["source_commit"] == canonical_source, row["patch"]
            assert mapped_lock["expected_tree"] == canonical_tree, row["patch"]
        if row["classification"] not in {
            "ALREADY_MIGRATED",
            "PUBLICATION_PENDING",
        }:
            assert "lock" not in row
            continue
        lock_path = ROOT / row["lock"]
        lock = load(lock_path)
        assert lock["upstream"]["url"] == row["upstream"]
        assert lock["mirror"]["url"] == row["downstream"]
        assert lock["upstream"]["base_commit"] == canonical_base
        assert lock["ordered_commits"] == canonical_commits
        assert lock["source_commit"] == canonical_source
        assert lock["expected_tree"] == canonical_tree
    host_tier = (ROOT / "ci/run-test-tier.sh").read_text().split("\thost)\n", 1)[1].split("\tguest-smoke)", 1)[0]
    runner = "tests/run-patch-stack-migration-inventory-contract.sh"
    assert runner in host_tier
    assert host_tier.index(runner) < host_tier.index("exec west test"), "inventory runner must precede west test"
    print(f"migration inventory contract: PASS ({len(rows)} series, {sum(r['commit_count'] for r in rows.values())} commits)")


if __name__ == "__main__":
    main()
