#!/usr/bin/env python3
"""Bind perf profile boundaries across the Rootless homebrew prerequisite."""
from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
LOCKS = ROOT / "locks" / "patch-stack"


def load(name: str) -> dict:
    value = yaml.safe_load((LOCKS / name).read_text())
    assert isinstance(value, dict) and value["schema_version"] == 2
    return value


def main() -> None:
    mapping = yaml.safe_load((LOCKS / "lock-first-series-perf-v2.yml").read_text())
    assert mapping["profile"] == "perf"
    assert mapping["batch_id"] == "darling-perf-lock-first-batch-1"
    assert mapping["expected_count"] == 7 == len(mapping["series"])
    observed = [(item["module"], item["patch"], item["lock"]) for item in mapping["series"]]
    assert observed == [
        ("darling", "darling/mldr-compact-fd-band.patch", "darling-mldr-compact-fd-band-v1.yml"),
        ("darling/src/external/xnu", "xnu/shmem-ring-guest.patch", "darling-xnu-shmem-ring-guest-profile-v7.yml"),
        ("darling/src/external/xnu", "xnu/j7e7-lane-wakefd-sentinel.patch", "darling-xnu-j7e7-lane-wakefd-sentinel-profile-v7.yml"),
        ("darling/src/external/dyld", "dyld/dcc2-reader.patch", "dyld-dcc2-reader-v1.yml"),
        ("darling/src/external/darlingserver", "darlingserver/perf18-server-ring.patch", "darlingserver-perf18-server-ring-rootless-profile-v9.yml"),
        ("darling/src/external/darlingserver", "darlingserver/a0-hang-fixes.patch", "darlingserver-a0-hang-fixes-rootless-profile-v9.yml"),
        ("darling/src/external/darlingserver", "darlingserver/j7e7-postfork-reset-gate.patch", "darlingserver-j7e7-postfork-reset-gate-rootless-profile-v9.yml"),
    ]
    mldr, shmem, sentinel, _dyld, perf18, hang, gate = [load(item[2]) for item in observed]
    assert mldr["upstream"]["base_commit"] == "50b2e05dd9e21d9f39e35d947f830ae651aa3366"
    assert mldr["source_commit"] == "93ba455b8e3d8ee3d04c712b3579c3d5b5e78fb7"
    assert mldr["expected_tree"] == "1fce0600b329d7f866872082268c8a9a98fbb8b9"
    composition = yaml.safe_load((LOCKS / "perf-profile-composition-v2.yml").read_text())
    assert composition["schema_version"] == 3
    prerequisite = composition["prerequisites"]
    assert [item["profile"] for item in prerequisite] == ["homebrew"]
    assert prerequisite[0]["module_trees"]["darling"] == "5e8144538bdc7cb7958dc22edc4016e8b64a6591"
    assert "source_oid" not in composition["modules"][0]["starting"]
    assert composition["modules"][0]["series"][0]["expected_applied_tree"] == "fbef26d273a9cefcc6fc2db72284e7c955356c2e"
    generated = {
        "43b4e876ad032635cfc5308ada0dc1bd383398b9",
        "585b0e89a7be83eaf8b8c0bd0ea7e69d1add0fea",
    }
    registry = yaml.safe_load((LOCKS / "lock-first-profiles-v1.yml").read_text())
    active_locks = set()
    for profile in registry["profiles"]:
        mapped = yaml.safe_load((LOCKS / profile["mapping"]).read_text())
        active_locks.update(item["lock"] for item in mapped["series"])
    assert "darling-mldr-compact-fd-band-profile-v7.yml" not in active_locks
    for name in active_locks:
        lock = load(name)
        values = {lock["upstream"]["base_commit"], lock["source_commit"], lock["mirror"]["base_oid"], lock["mirror"]["source_oid"], lock["mirror"]["base_ref"], lock["mirror"]["source_ref"]}
        assert not values & generated, (name, values & generated)
    assert len(shmem["ordered_commits"]) == 17
    assert shmem["source_commit"] == sentinel["upstream"]["base_commit"]
    assert sentinel["source_commit"] == "c85e7afea09c414ecc15e17175076f8d323068fe"
    assert perf18["upstream"]["base_commit"] == "4306b73eb20d08ddc9d544672d65d16326796e58"
    assert perf18["source_commit"] == hang["upstream"]["base_commit"] == "a327ca32906c3729cf3a0178c250f0c90c69f291"
    assert hang["source_commit"] == gate["upstream"]["base_commit"] == "85cc586e19043e71811e42c8f270625568b89f1c"
    assert gate["source_commit"] == "0d4b252c566d064b954ca913813ec9fa2bf6cb20"

    arch = yaml.safe_load((LOCKS / "lock-first-series-arch-v2.yml").read_text())
    arch_xnu = [item["lock"] for item in arch["series"] if item["module"] == "darling/src/external/xnu"]
    assert arch_xnu == [
        "darling-xnu-ring-committed-unknown-guest-profile-v7.yml",
        "darling-xnu-rpc-interruptible-disconnect-status-profile-v7.yml",
        "darling-xnu-rpc-disconnect-status-contract-test-profile-v7.yml",
    ]
    first_arch_xnu = load(arch_xnu[0])
    assert first_arch_xnu["upstream"]["base_commit"] == "92cc4fe7a5eff5027bafa16e7973484eb0aaa12f"
    assert first_arch_xnu["expected_tree"] == "8483bec867bfd01e7f034ef08e0295c28a36122b"
    assert load("darling-xnu-ring-committed-unknown-guest-v1.yml")["expected_tree"] != first_arch_xnu["expected_tree"]

    forensic = yaml.safe_load((LOCKS / "archive-forensic-exceptions-v1.yml").read_text())
    assert any(item["classification"] == "LEGACY_ARCHIVE_NOT_CLEAN_ODB_REPRODUCIBLE"
               and item["profile"] == "perf" for item in forensic["exceptions"])
    doc = (ROOT / "docs" / "perf-v7-profile-integration-readiness.md").read_text()
    assert "9d525a42d14010ec55a1cdfa1bcf782e6f259948" in doc
    audit = (ROOT / "docs" / "xnu-v6-v7-semantic-audit.md").read_text()
    assert "ACCEPT_V7" in audit
    assert "SEMANTIC_CHANGE" in audit and "There are no\n`SEMANTIC_CHANGE` paths." in audit
    assert "b329716e3113dca8e40e49d2d282254a07b74051" in audit
    assert "e1df84b1db082eb5fa11fdab7414c07aa4d29ca1" in audit
    assert "327c327522a3559f08c7d3f70398cc6fff15acee" in audit
    print("perf profile-integration contract: PASS")


if __name__ == "__main__":
    main()
