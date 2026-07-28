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

    lock = yaml.safe_load((ROOT / "locks/patch-stack/xnu-perf-v1.yml").read_text())
    assert lock["schema_version"] == 2
    assert lock["upstream"]["base_commit"] == "e1db4266f50415c013371fc57e8f38a0423493ec"
    assert lock["source_commit"] == "88dcbf670cd4d1c000dd7f7d95324784bafb0dca"
    assert lock["expected_tree"] == "c0b2c145f7f26734853657b165da88cc51ec7f46"
    assert len(lock["ordered_commits"]) == 17

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
        "lock": "xnu-perf-v1.yml",
        "missing_blob": BLOB,
        "index_path": "darling/src/libsystem_kernel/emulation/src/linux_premigration/vchroot_userspace.c",
        "index_preimage": BLOB,
        "declared_base": lock["upstream"]["base_commit"],
        "declared_source": lock["source_commit"],
        "authority": "immutable-cherry-pick-oracle",
    }]

    doc = (ROOT / "docs/legacy-runtime-retirement-audit.md").read_text()
    assert "LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE" in doc
    assert BLOB in doc
    assert "immutable-cherry-pick-oracle" in doc
    assert "Arch integration topology exception" in doc
    assert "80e8f944c0148e93f2b6f0a1501b8de7e8a3aa47" in doc
    assert "d415bb9218bd066f36bc54d6d6fb4abb5508dfc5" in doc
    print("perf archive forensic contract: PASS")


if __name__ == "__main__":
    main()
