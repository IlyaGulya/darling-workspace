#!/usr/bin/env python3
"""Fail-closed census for production legacy-runtime retirement inputs."""
from __future__ import annotations

import json
import sys
from collections import OrderedDict
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
LOCKS = ROOT / "locks" / "patch-stack"
sys.path.insert(0, str(ROOT / "west_commands"))
import patch_stack_materialize
import patch_stack_lock_first


def fail(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    inventory = json.loads((LOCKS / "legacy-runtime-profile-inventory-v1.json").read_text())
    fail(set(inventory) == {"schema_version", "source", "profiles", "closure"}, "inventory top-level fields changed")
    fail(inventory["schema_version"] == 1, "inventory schema version changed")
    fail(inventory["closure"] == {
        "total_series": 95,
        "total_ordered_commits": 187,
        "schema_v2_lock_coverage": "94/94 hosted plus 1 publication-pending Arch continuation",
        "immutable_ref_closure": "hosted_immutable_clean_odb plus publication-pending Arch continuation",
        "clean_odb_evidence": "94 hosted series independently verified; b545 Arch continuation has dual local clean-ODB evidence pending create-only hosted tags",
    }, "inventory closure no longer states complete immutable coverage")
    profiles = inventory["profiles"]
    fail(isinstance(profiles, list) and [row.get("profile") for row in profiles] == ["homebrew", "perf", "arch"], "inventory profile order changed")
    expected = {
        "homebrew": (69, 97, None, "darling-homebrew-lock-first-batch-7"),
        "perf": (7, 29, "homebrew", "darling-perf-lock-first-batch-1"),
        "arch": (19, 61, "perf", "darling-arch-lock-first-batch-1"),
    }
    forensic = yaml.safe_load((LOCKS / "archive-forensic-exceptions-v1.yml").read_text())
    fail(set(forensic) == {"schema_version", "exceptions"}, "forensic exception schema changed")
    fail(forensic["schema_version"] == 1 and isinstance(forensic["exceptions"], list), "forensic exception inventory invalid")
    nonportable = {
        (row.get("profile"), row.get("patch")): row
        for row in forensic["exceptions"]
    }
    total_series = total_commits = 0
    for row in profiles:
        profile = row["profile"]
        series_count, commit_count, base_profile, batch_id = expected[profile]
        fail(row.get("series_count") == series_count, f"{profile}: inventory series count")
        fail(row.get("ordered_commit_count") == commit_count, f"{profile}: inventory ordered commit count")
        fail(row.get("base_profile") == base_profile, f"{profile}: inventory base profile")
        profile_data = yaml.safe_load((ROOT / "patches" / profile / "patches.yml").read_text())
        fail(profile_data.get("base-profile") == base_profile, f"{profile}: patches metadata base profile")
        patches = profile_data.get("patches")
        fail(isinstance(patches, list) and len(patches) == series_count, f"{profile}: patches count")
        grouped: OrderedDict[str, list[dict[str, str]]] = OrderedDict()
        for patch in patches:
            grouped.setdefault(patch["module"], []).append(patch)
        mapping_path = LOCKS / row["mapping"]
        fail(patch_stack_lock_first.mapping_for_profile(profile) == mapping_path.resolve(), f"{profile}: registry mapping")
        plan = patch_stack_lock_first.plan(profile, patches)
        fail(plan.batch["batch_id"] == batch_id and plan.batch["expected_count"] == series_count, f"{profile}: typed batch identity")
        expected_order = [(module, patch["path"]) for module, patches in grouped.items() for patch in patches]
        fail([(entry["module"], entry["patch"]) for entry in plan] == expected_order, f"{profile}: mapping order differs from grouped execution")
        fail(plan.batch["module_order"] == list(grouped), f"{profile}: mapping module order")
        entries = {(entry["module"], entry["patch"]): entry for entry in plan}
        fail(len(entries) == len(plan), f"{profile}: duplicate typed mapping identity")
        observed_commits = 0
        for patch in patches:
            entry = entries.get((patch["module"], patch["path"]))
            fail(entry is not None, f"{profile}/{patch['path']}: typed mapping entry missing")
            lock = patch_stack_materialize.load_lock(Path(entry["lock_path"]))
            fail(lock["schema_version"] == 2, f"{profile}/{patch['path']}: not schema-v2")
            exception = nonportable.get((profile, patch["path"]))
            if exception is not None:
                fail(
                    exception.get("classification") == "LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE",
                    f"{profile}/{patch['path']}: unapproved source divergence",
                )
                fail(
                    exception.get("declared_source") == patch["source-commit"],
                    f"{profile}/{patch['path']}: forensic source does not match archive provenance",
                )
                fail(
                    exception.get("authority") == "immutable-cherry-pick-oracle",
                    f"{profile}/{patch['path']}: nonportable archive lacks canonical authority",
                )
                fail(
                    exception.get("lock") == Path(entry["lock_path"]).name,
                    f"{profile}/{patch['path']}: forensic exception does not name canonical lock",
                )
                fail(
                    lock["source_commit"] != patch["source-commit"],
                    f"{profile}/{patch['path']}: nonportable archive unexpectedly became canonical source",
                )
            fail(lock["mirror"]["source_oid"] == lock["source_commit"], f"{profile}/{patch['path']}: source OID differs")
            fail(lock["mirror"]["base_oid"] == lock["upstream"]["base_commit"], f"{profile}/{patch['path']}: base OID differs")
            fail(lock["ordered_commits"][-1] == lock["source_commit"], f"{profile}/{patch['path']}: ordered tip differs")
            observed_commits += len(lock["ordered_commits"])
        fail(observed_commits == commit_count, f"{profile}: ordered commit total")
        total_series += series_count
        total_commits += commit_count
    fail((total_series, total_commits) == (95, 187), "inventory totals differ")
    fail(
        set(nonportable) == {("perf", "xnu/shmem-ring-guest.patch")},
        "unexpected archive forensic exception",
    )
    print("legacy-runtime inventory contract: PASS")


if __name__ == "__main__":
    main()
