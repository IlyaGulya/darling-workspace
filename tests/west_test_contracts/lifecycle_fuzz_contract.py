"""Bounded contract for the Rust-owned dar-4ush.4 fuzz harness.

This module is transport/contract code only.  Bytecode generation, replay,
coverage and recovery classification remain in the Rust crate.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAX_OUTPUT = 64 * 1024
MAX_INPUT = 4096
EXPECTED_EXPLORER_COMMIT = "d02570556cac7df68459fe2e7df4f189b665b955"
EXPECTED_EXPLORER_TREE = "1daf503705aea74c7a6a2d7b94205409d164b9b5"

binary = Path(
    os.environ.get(
        "DARLING_LIFECYCLE_FUZZ_BIN",
        ROOT / "lifecycle" / "operation-boundary" / "target" / "debug" / "lifecycle-fuzz",
    )
)
owned_root = Path(os.environ["DARLING_LIFECYCLE_FUZZ_TMPDIR"]).resolve()
assert owned_root.is_dir()
assert Path(os.environ["TMPDIR"]).resolve() == owned_root

target_source = (ROOT / "lifecycle" / "operation-boundary" / "fuzz" / "fuzz_targets" / "lifecycle.rs").read_text(encoding="utf-8")
assert "safe_replay" in target_source
assert "fuzz_one" not in target_source
assert "dispose_replay_roots" in target_source
assert "MAX_CAMPAIGN_FORENSIC_ROOTS" in target_source


def run(*args: str, input_bytes: bytes | None = None) -> tuple[int, str, str]:
    process = subprocess.run(
        [str(binary), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        env={**os.environ, "TMPDIR": str(owned_root)},
    )
    stdout = process.stdout[:MAX_OUTPUT]
    stderr = process.stderr[:MAX_OUTPUT]
    assert len(process.stdout) <= MAX_OUTPUT, "fuzz output exceeded the contract bound"
    assert len(process.stderr) <= MAX_OUTPUT, "fuzz stderr exceeded the contract bound"
    return process.returncode, stdout.decode(), stderr.decode()


def json_command(*args: str) -> dict:
    code, stdout, stderr = run(*args)
    assert code == 0, (args, code, stderr)
    payload = json.loads(stdout)
    assert len(stdout.encode()) <= MAX_OUTPUT
    assert payload.get("status") != "PANIC"
    census = payload.get("resource_census")
    if census is not None:
        assert census["events"] <= 64
        assert census["schedule_limit"] <= 64
        assert census["recovery_limit"] <= 16
        assert census["filesystem_roots_created"] <= 8
        assert len(payload.get("forensic_roots", [])) <= 1
    return payload


def reducer_hex(ops: bytes, operation_count: int, seed: int = 0x5445524D) -> str:
    """Encode a reducer program with an explicit operation count."""
    assert 0 <= operation_count <= 64
    header = (
        b"DLF1"
        + bytes([1, 0])
        + seed.to_bytes(8, "little")
        + bytes([0, 0, 2, operation_count])
    )
    return (header + ops).hex()


def assert_source_identity(payload: dict) -> None:
    identity = payload["source_identity"]
    assert identity["repository"] == "darling-workspace"
    assert identity["module"] == "lifecycle-fuzz"
    assert identity["bytecode_version"] == 1
    assert identity["explorer_commit"] == EXPECTED_EXPLORER_COMMIT
    assert identity["explorer_tree"] == EXPECTED_EXPLORER_TREE
    assert identity["explorer_identity_role"] == "provenance-anchor-only"
    assert len(identity["fuzz_source_sha256"]) == 64
    assert len(identity["fuzz_target_sha256"]) == 64
    assert identity["semantic_closure_authoritative"] is True
    assert identity["semantic_closure_scope"] == "reducer-explorer-policy-schema-contract-locks"
    assert len(identity["semantic_closure_sha256"]) == 64
    closure = {entry["path"]: entry["sha256"] for entry in identity["semantic_closure"]}
    required_closure = {
        "build.rs",
        "Cargo.toml",
        "Cargo.lock",
        "src/fuzz.rs",
        "src/explorer.rs",
        "src/state.rs",
        "src/lib.rs",
        "src/bin/lifecycle-fuzz.rs",
        "src/bin/lifecycle-boundary.rs",
        "fuzz/fuzz_targets/lifecycle.rs",
        "fuzz/Cargo.toml",
        "fuzz/Cargo.lock",
        "../../lifecycle/operation-boundary-v1.json",
        "../../lifecycle/state-model-v1.json",
        "../../docs/lifecycle-fuzzing-v1.md",
        "../../tests/west_test_contracts/lifecycle_fuzz_contract.py",
        "../../tests/west_test_contracts/lifecycle_explorer_contract.py",
        "../../tests/run-lifecycle-fuzz-ub-gate.sh",
        "../../tests/run-lifecycle-fuzz-contract.sh",
    }
    assert required_closure <= closure.keys()
    assert all(len(value) == 64 for value in closure.values())
    assert identity["workspace_head"]


def assert_owned_path(path_value: str) -> Path:
    path = Path(path_value).resolve()
    path.relative_to(owned_root)
    assert path.is_dir()
    manifest = path / "lifecycle-fuzz-manifest.json"
    assert manifest.is_file()
    manifest_payload = json.loads(manifest.read_text())
    assert manifest_payload["schema"] == "darling.lifecycle-fuzz.forensic-manifest.v1"
    assert manifest_payload["recovery_status"] == "FORENSIC_REQUIRED"
    assert_source_identity(manifest_payload)
    return path


verify = json_command("--verify-corpus")
assert verify["corpus_count"] == 38
assert verify["golden_count"] == 6
assert verify["forensic_count"] == 24
assert verify["historical_count"] == 8
assert verify["historical_replayed"] == 8
assert verify["historical_bad_arm_reproduced"] == 8
assert verify["historical_bad_arm_unique"] == 8
assert verify["unique_programs"] == 38
assert verify["forensic_reproduced"] == 24
assert len(verify["forensic_roots"]) == 8
assert_source_identity(verify)

expected_golden = {
    "shared-session-signal-gone",
    "shared-session-signal-rejected",
    "shared-session-retained-holder-timeout",
    "shared-session-pid-reuse",
    "shared-session-late-fork",
    "session-root-exit-before-snapshot",
}
expected_historical = {
    "historical-inode-aba",
    "historical-pid-reuse",
    "historical-late-fork",
    "historical-deadline",
    "historical-rejected-signal",
    "historical-root-gone",
    "historical-cleanup-failure",
    "historical-quarantine-replacement",
}
assert expected_golden | expected_historical <= set(verify["names"])
assert len(verify["historical_provenance"]) == 8
assert len(verify["historical_bad_violations"]) == 8
assert len({item["expected_class"] for item in verify["historical_bad_violations"]}) == 8
assert len(
    {
        tuple(sorted(item["expected_invariants"]))
        for item in verify["historical_bad_violations"]
    }
) == 8
for violation in verify["historical_bad_violations"]:
    expected = set(violation["expected_invariants"])
    actual = set(violation["actual"]["missing_invariants"])
    assert expected
    # A historical RED arm is typed, not merely any program that happens to
    # violate one of its invariants.  The exact missing set is part of the
    # provenance contract, so a generic shared-lease arm cannot stand in for
    # an inode, PID, deadline, or recovery failure.
    assert expected == actual
    assert violation["actual"]["status"] == "INVARIANT_VIOLATION"
assert any("rootless" in item for item in verify["historical_provenance"])
assert any("E-UNION" in item for item in verify["historical_provenance"])
assert any("deploy" in item for item in verify["historical_provenance"])
assert any("split-lock" in item for item in verify["historical_provenance"])
assert any("stale-index" in item for item in verify["historical_provenance"])
assert any("verifier-regression" in item for item in verify["historical_provenance"])
for path in verify["forensic_roots"]:
    assert_owned_path(path)

smoke = json_command("--smoke", "--max-cases", "64")
assert smoke["status"] == "FUZZ_SMOKE_VALID"
assert smoke["max_cases"] == 64
assert smoke["cases"] <= 64
assert smoke["cases"] > 14
assert smoke["mutated_cases"] > 0
assert smoke["unique_coverage"] > 0
assert 0 < smoke["rejected"] <= smoke["mutated_cases"]
assert smoke["explorer_observed"] > 0
assert (
    smoke["accepted"]
    + smoke["rejected"]
    + smoke["incomplete"]
    + smoke["budget_exceeded"]
    + smoke["invariant_failures"]
    + smoke["explorer_observed"]
    == smoke["cases"]
)
assert smoke["budget_exceeded"] == 0
assert smoke["invariant_failures"] == 0
assert_source_identity(smoke)
assert_source_identity(smoke["corpus"])
for path in smoke["corpus"]["forensic_roots"]:
    assert_owned_path(path)

# Deterministic decode -> encode -> replay uses the Rust registry to obtain a
# seed, then requires byte-identical reports from two independent replays.
code, golden_hex, stderr = run("--corpus-hex", "shared-session-signal-gone")
assert code == 0, stderr
golden_hex = golden_hex.strip()
first = json_command("--replay-hex", golden_hex)
second = json_command("--replay-hex", golden_hex)
assert first == second
assert first["status"] == "ACCEPTED"
assert first["minimized_bytecode_hex"] == golden_hex
assert first["resource_census"]["input_bytes"] <= MAX_INPUT

# A rejected reducer program must carry a bounded minimized replay trace.  The
# first golden opcode is Intent; changing only its tag to Acquire preserves
# bytecode validity while producing a real reducer rejection.
rejected_bytes = bytearray.fromhex(golden_hex)
rejected_bytes[18] = 2
rejected = json_command("--replay-hex", rejected_bytes.hex())
assert rejected["status"] == "REJECTED"
assert rejected["rejected_events"] >= 1
assert rejected["minimized_bytecode_hex"]
assert len(rejected["minimized_replay_trace"]) <= rejected["event_count"]
assert rejected["failure_fingerprint"]["status"] == "REJECTED"
assert rejected["failure_fingerprint"]["error_classes"]

# Terminal is a strict final event, including bytecode operations that do not
# become reducer events.  These are rejected traces, not invariant crashes.
for suffix, operation_count in (
    (bytes([11, 1, 13]), 2),
    (bytes([11, 1, 12, 1, 0]), 2),
):
    terminal_suffix = json_command(
        "--replay-hex", reducer_hex(suffix, operation_count)
    )
    assert terminal_suffix["status"] == "REJECTED"
    assert terminal_suffix["rejected_events"] >= 1
    assert any("event after terminal" in error for error in terminal_suffix["errors"])

# A virtual-time budget failure with an unresolved typed deadline remains a
# budget result; it must not be promoted to an invariant crash artifact.
budget_ops = bytes([0, 3, 1, 2, 0, 3, 0, 1, 5, 0, 0, 3]) + bytes([12, 0xFF, 0xFF]) * 16
budget = json_command(
    "--replay-hex", reducer_hex(budget_ops, 21, seed=0x425544474554)
)
assert budget["status"] == "BUDGET_EXCEEDED"
assert budget["status"] != "INVARIANT_VIOLATION"
assert any("virtual time budget exceeded" in error for error in budget["errors"])

# Malformed, truncated and oversized stdin are typed decode rejections, never
# process crashes or unbounded filesystem activity.
malformed_inputs = [b"", b"DLF1", b"not-bytecode", b"x" * (MAX_INPUT + 1)]
for malformed in malformed_inputs:
    code, stdout, stderr = run(input_bytes=malformed)
    assert code == 0, (len(malformed), stderr)
    report = json.loads(stdout)
    assert report["status"] == "DECODE_REJECTED"
    assert report["resource_census"]["input_bytes"] == len(malformed)
    assert report["resource_census"]["input_bytes"] <= MAX_INPUT + 1
    assert report["forensic_roots"] == []

forensic_selectors = [
    (5, 3),
    (5, 4),
    (5, 6),
    (11, 3),
    (11, 4),
    (11, 6),
    (12, 3),
    (12, 6),
    (8, 1),
    (8, 2),
    (9, 1),
    (9, 2),
    (10, 1),
    (10, 2),
    (11, 1),
    (11, 2),
    (12, 1),
    (12, 2),
    (13, 1),
    (13, 2),
    (14, 1),
    (14, 2),
    (15, 1),
    (15, 2),
]
for boundary, interleaving in forensic_selectors:
    first = json_command("--explorer-case", "7", str(boundary), str(interleaving))
    second = json_command("--explorer-case", "7", str(boundary), str(interleaving))
    assert first["scenario"] == second["scenario"]
    assert first["recovery_status"] == "FORENSIC_REQUIRED"
    assert first["status"] == "EXPLORER_OBSERVED"
    assert first["minimized_replay_trace"] == second["minimized_replay_trace"]
    for path in first["forensic_roots"]:
        assert_owned_path(path)

# Every retained directory belongs to the task-owned namespace and has its
# typed manifest.  CLEAN roots are removed by the Rust explorer and therefore
# do not appear here.
for child in owned_root.iterdir():
    if child.is_dir():
        assert_owned_path(str(child))
assert len([child for child in owned_root.iterdir() if child.is_dir()]) <= 64

print(
    json.dumps(
        {
            "status": "LIFECYCLE_FUZZ_CONTRACT_VALID",
            "corpus": 38,
            "golden": 6,
            "forensic": 24,
            "historical": 8,
            "forensic_roots_manifested": len(
                [child for child in owned_root.iterdir() if child.is_dir()]
            ),
            "deterministic_replay": True,
            "malformed_rejections": len(malformed_inputs),
        },
        sort_keys=True,
    )
)
