#!/usr/bin/env python3
"""Fail-closed policy for operational retirement of archive materialization."""
from __future__ import annotations

import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
PROFILE_COUNTS = {"homebrew": 72, "perf": 7, "arch": 19}
MAPPINGS = {
    "homebrew": "lock-first-series-v2.yml",
    "perf": "lock-first-series-perf-v2.yml",
    "arch": "lock-first-series-arch-v2.yml",
}
ARCHIVE_CONSUMERS = {
    ("profile-checksum-provenance", "west_commands/patch.py", "checksum-and-source-export-drift", "read"),
    ("west-command-authoring-export", "west_commands/patch.py", "authoring-output-refresh", "write"),
    ("authoring-export", "scripts/export_patches.py", "authoring-output-refresh", "write"),
    (
        "pr-review-provenance",
        "west_commands/pr.py",
        "dashboard-and-publish-checksum",
        "read",
    ),
    (
        "guest-macho-owner-metadata",
        "ci/guest_macho_batch_specs.py",
        "owning-patch-path-and-checksum-metadata",
        "read",
    ),
    (
        "guest-macho-owner-checksum",
        "ci/build-guest-macho-batch.sh",
        "owning-patch-checksum-verification",
        "read",
    ),
    (
        "guest-macho-source-provenance",
        "tests/west_test_contracts/macho_corpus_batch_contract.py",
        "reviewed-source-provenance-comparison",
        "read",
    ),
    (
        "lock-first-rollback-fixture",
        "tests/west_test_contracts/patch_stack_lock_first_contract.py",
        "non-reading-owning-path-fixture",
        "read",
    ),
    (
        "perf-nonportable-forensic",
        "tests/west_test_contracts/perf_archive_forensic_contract.py",
        "read-only-index-stanza-classification",
        "read",
    ),
    (
        "migration-inventory",
        "tests/west_test_contracts/patch_stack_migration_inventory_contract.py",
        "checksum-and-inventory",
        "read",
    ),
}


def main() -> None:
    locks = ROOT / "locks" / "patch-stack"
    registry = yaml.safe_load((locks / "lock-first-profiles-v1.yml").read_text())
    assert registry == {
        "schema_version": 1,
        "profiles": [
            {"profile": "homebrew", "mapping": MAPPINGS["homebrew"]},
            {"profile": "arch", "mapping": MAPPINGS["arch"]},
            {"profile": "perf", "mapping": MAPPINGS["perf"]},
        ],
    }
    for profile, expected_count in PROFILE_COUNTS.items():
        mapping = yaml.safe_load((locks / MAPPINGS[profile]).read_text())
        assert mapping["schema_version"] == 3
        assert mapping["profile"] == profile
        assert mapping["expected_count"] == expected_count
        assert len(mapping["series"]) == expected_count
        assert len(
            {(entry["module"], entry["patch"]) for entry in mapping["series"]}
        ) == expected_count

    oracle_registry = yaml.safe_load(
        (locks / "immutable-oracle-profiles-v1.yml").read_text()
    )
    assert oracle_registry["schema_version"] == 1
    assert {
        (
            entry["profile"],
            entry["oracle_mode"],
            entry["mapping"],
        )
        for entry in oracle_registry["profiles"]
    } == {
        (profile, "immutable-cherry-pick-oracle", mapping)
        for profile, mapping in MAPPINGS.items()
    }

    archive_registry = yaml.safe_load(
        (locks / "archive-consumers-v1.yml").read_text()
    )
    assert archive_registry["schema_version"] == 1
    assert (
        archive_registry["classification"]
        == "NON_EXECUTABLE_ARCHIVE_PROVENANCE_RECOVERY"
    )
    assert {
        (entry["id"], entry["path"], entry["operation"], entry["access"])
        for entry in archive_registry["consumers"]
    } == ARCHIVE_CONSUMERS
    assert all(
        entry["executes_archive"] is False
        and entry["access"] in {"read", "write"}
        and set(entry)
        == {"id", "path", "operation", "access", "executes_archive"}
        and (ROOT / entry["path"]).is_file()
        for entry in archive_registry["consumers"]
    )
    assert {
        entry["id"]
        for entry in archive_registry["consumers"]
        if entry["access"] == "write"
    } == {"west-command-authoring-export", "authoring-export"}
    declared_consumers = {
        entry["path"] for entry in archive_registry["consumers"]
    }
    # Literal references to versioned archive paths are an intentionally
    # narrower grep surface than dynamic profile readers.  Every such caller
    # must be typed above; the dynamic readers are asserted explicitly below.
    literal_callers = set()
    archive_literal = re.compile(
        r"""patches/(?:homebrew|perf|arch)/[^"' \t\r\n]+\.patch"""
    )
    for directory in ("ci", "scripts", "tests", "west_commands"):
        for path in (ROOT / directory).glob("**/*"):
            if (
                path.is_file()
                and path.suffix in {".py", ".sh"}
                and path != Path(__file__)
            ):
                source = path.read_text()
                if archive_literal.search(source):
                    literal_callers.add(str(path.relative_to(ROOT)))
    assert literal_callers <= declared_consumers, sorted(
        literal_callers - declared_consumers
    )
    required_access_markers = {
        "profile-checksum-provenance": (
            "west_commands/patch.py",
            'profile_dir / patch["path"]',
            "patch_path.read_bytes()",
        ),
        "west-command-authoring-export": (
            "west_commands/patch.py",
            "output.write_bytes(plan.exported)",
            "_update_profile_metadata(profile_path, metadata_updates)",
        ),
        "pr-review-provenance": (
            "west_commands/pr.py",
            'self.profile_path.parent / patch["path"]',
            "patch_path.read_bytes()",
        ),
        "authoring-export": (
            "scripts/export_patches.py",
            'profile_dir / patch["path"]',
            "output.read_bytes()",
        ),
        "guest-macho-owner-metadata": (
            "ci/guest_macho_batch_specs.py",
            "patch_path: str",
            "patch_sha256: str",
        ),
        "guest-macho-owner-checksum": (
            "ci/build-guest-macho-batch.sh",
            'sha256sum -- "$patch_file"',
            "actual_patch_sha256",
        ),
        "guest-macho-source-provenance": (
            "tests/west_test_contracts/macho_corpus_batch_contract.py",
            "extract_added_file(REVIEWED_XNU_PATCH",
            "WORKSPACE_ABORT_SOURCE.read_bytes()",
        ),
        "lock-first-rollback-fixture": (
            "tests/west_test_contracts/patch_stack_lock_first_contract.py",
            'ROOT / "patches/homebrew/xnu/eunion-hardening.patch"',
        ),
        "perf-nonportable-forensic": (
            "tests/west_test_contracts/perf_archive_forensic_contract.py",
            '"patches/perf/xnu/shmem-ring-guest.patch"',
            ").read_text()",
        ),
        "migration-inventory": (
            "tests/west_test_contracts/patch_stack_migration_inventory_contract.py",
            'ROOT / "patches" / profile / item["path"]',
            "artifact.read_text()",
        ),
    }
    assert set(required_access_markers) == {
        entry["id"] for entry in archive_registry["consumers"]
    }
    for _consumer, (relative, *markers) in required_access_markers.items():
        source = (ROOT / relative).read_text()
        assert all(marker in source for marker in markers), relative

    production_files = [
        *sorted((ROOT / ".github" / "workflows").glob("*.yml")),
        *sorted((ROOT / "ci").glob("*.py")),
        *sorted((ROOT / "ci").glob("*.sh")),
        *sorted((ROOT / "west_commands").glob("*.py")),
    ]
    production = "\n".join(path.read_text() for path in production_files)
    for forbidden in (
        "--legacy-mbox",
        "--shadow-lock",
        "--shadow-evidence",
        "patch_stack_legacy_oracle",
        "patch_stack_shadow",
        "git_for_patch_application",
        "git_for_temporary_patch_application",
    ):
        assert forbidden not in production, forbidden

    workflow = (ROOT / ".github/workflows/patch-stack-lock-first.yml").read_text()
    assert "tests/patch_stack_immutable_oracle.py" in workflow
    assert "compare-immutable-oracle" in workflow
    assert "immutable-oracle.json" in workflow
    assert "workflow_dispatch:" in workflow and "\n  push:" not in workflow
    assert not (ROOT / ".github/workflows/patch-stack-shadow.yml").exists()

    host = (ROOT / "ci/run-test-tier.sh").read_text()
    assert "west test --profile homebrew --env host --materialize-profile" in host
    runtime_source = (ROOT / "west_commands/test_runtime_source.py").read_text()
    assert 'profile not in {"homebrew", "perf", "arch"}' in runtime_source
    assert "skip_patches=phase_skips" in runtime_source
    assert "Historical archives are never executable inputs." in runtime_source

    patch_command = (ROOT / "west_commands/patch.py").read_text()
    assert patch_command.count('"--roll-back"') == 1
    assert "deprecated compatibility no-op" in patch_command
    assert "args.roll_back" not in patch_command
    assert "patch_stack_export.export_profile" in patch_command
    assert "Replay the complete typed profile graph in disposable worktrees." in patch_command
    assert 'for prerequisite in composition["prerequisites"]' in patch_command
    assert "reset_to_first_base=module not in materialized" in patch_command
    assert "def _patch_state" not in patch_command
    assert '["git", "apply"' not in patch_command
    assert "Report canonical integration state without executing archives." in patch_command
    assert "PATCH_STACK_MODE=default-lock-first" not in patch_command
    assert 'patch_stack_mode = "default-lock-first"' in patch_command

    archives = list((ROOT / "patches").glob("**/*.patch"))
    assert archives, "historical archives were removed"
    print("patch-stack retirement policy contract: PASS")


if __name__ == "__main__":
    main()
