#!/usr/bin/env python3
"""Keep the non-portable perf archive classification explicit and typed."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
BLOB = "024ca655535df09af16e052c927001ac50484aff"


def main() -> None:
    archive = (ROOT / "patches/perf/xnu/shmem-ring-guest.patch").read_text()
    assert f"index {BLOB}..dfcb460aa664fc3df2e0eff2d398e05d9e8e3e10 100644" in archive
    assert "vchroot_userspace.c" in archive

    # The perf profile materializes from the lock the mapping names, never from
    # the archive classified below.
    mapping = yaml.safe_load((ROOT / "locks/patch-stack/lock-first-series-perf-v2.yml").read_text())
    mapped = {
        (entry["profile"], entry["patch"]): entry
        for entry in mapping["series"]
    }
    canonical = mapped[("perf", "xnu/shmem-ring-guest.patch")]
    assert canonical["lock"] == "darling-xnu-shmem-ring-guest-profile-v7.yml"

    lock = yaml.safe_load((ROOT / "locks/patch-stack" / canonical["lock"]).read_text())
    assert lock["schema_version"] == 2
    assert lock["upstream"]["base_commit"] == "3313e58b4ef0ac449db7b4aea6a454f6b4de5a9c"
    assert lock["source_commit"] == "991fa4e4ee5e51e736c7fc27531f0e577917126e"
    assert lock["expected_tree"] == "03a593d3131f7f57949473f18399a7b28fc998a9"
    assert len(lock["ordered_commits"]) == 17

    profile = yaml.safe_load((ROOT / "patches/perf/patches.yml").read_text())
    declared = {
        (entry["module"], entry["path"]): entry for entry in profile["patches"]
    }[("darling/src/external/xnu", "xnu/shmem-ring-guest.patch")]
    # The archive declares the historical boundary its export was cut from;
    # the profile-integration lock is a different, canonical source.
    assert declared["source-base"] == "e1db4266f50415c013371fc57e8f38a0423493ec"
    assert declared["source-commit"] == "88dcbf670cd4d1c000dd7f7d95324784bafb0dca"
    assert lock["upstream"]["base_commit"] != declared["source-base"]
    assert lock["source_commit"] != declared["source-commit"]

    registry = yaml.safe_load((ROOT / "locks/patch-stack/immutable-oracle-profiles-v1.yml").read_text())
    assert registry["schema_version"] == 1
    by_profile = {entry["profile"]: entry for entry in registry["profiles"]}
    assert by_profile["perf"] == {
        "profile": "perf",
        "oracle_mode": "immutable-cherry-pick-oracle",
        "mapping": "lock-first-series-perf-v2.yml",
    }
    assert by_profile["homebrew"]["oracle_mode"] == "immutable-cherry-pick-oracle"
    assert by_profile["arch"]["oracle_mode"] == "immutable-cherry-pick-oracle"

    exceptions = yaml.safe_load((ROOT / "locks/patch-stack/archive-forensic-exceptions-v1.yml").read_text())
    assert exceptions["schema_version"] == 1
    assert exceptions["exceptions"] == [{
        "profile": "perf",
        "patch": "xnu/shmem-ring-guest.patch",
        "artifact": "patches/perf/xnu/shmem-ring-guest.patch",
        "classification": "LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE",
        "lock": canonical["lock"],
        "missing_blob": BLOB,
        "index_path": "darling/src/libsystem_kernel/emulation/src/linux_premigration/vchroot_userspace.c",
        "index_preimage": BLOB,
        "declared_base": declared["source-base"],
        "declared_source": declared["source-commit"],
        "authority": "immutable-cherry-pick-oracle",
    }]

    # The conflict bundles the audit records are superseded provenance, held in
    # the historical v1 lock, and are not the mapping's canonical arch lock.
    arch_mapping = yaml.safe_load((ROOT / "locks/patch-stack/lock-first-series-arch-v2.yml").read_text())
    arch_mapped = {
        (entry["profile"], entry["patch"]): entry
        for entry in arch_mapping["series"]
    }
    arch_canonical = arch_mapped[("arch", "darlingserver/a0-arch-redesign.patch")]
    assert arch_canonical["lock"] == "darlingserver-a0-arch-redesign-profile-v6.yml"
    historical = yaml.safe_load((ROOT / "locks/patch-stack/darlingserver-a0-arch-redesign-v1.yml").read_text())
    assert "80e8f944c0148e93f2b6f0a1501b8de7e8a3aa47" in historical["ordered_commits"]
    assert historical["expected_tree"] == "d415bb9218bd066f36bc54d6d6fb4abb5508dfc5"

    doc = (ROOT / "docs/legacy-runtime-retirement-audit.md").read_text()
    assert "LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE" in doc
    assert BLOB in doc
    assert "Perf forensic exception" in doc
    assert "independent immutable cherry-pick" in doc
    assert "Resolved Arch topology" in doc
    assert "The historical conflict bundles remain provenance" in doc
    transition = (ROOT / "docs/patch-stack-lock-first-transition.md").read_text()
    assert "immutable-cherry-pick-oracle" in transition
    print("perf archive forensic contract: PASS")


if __name__ == "__main__":
    main()
