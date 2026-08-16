#!/usr/bin/env python3
"""Architecture gate for the single FD-relative guest-namespace pilot."""

from pathlib import Path
import json
import os
import resource
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
TASK = ROOT.parent

policy = json.loads((ROOT / "lifecycle/guest-namespace-authority-v1.json").read_text())
assert policy["default"] == "OFF" and policy["routing"] == "DEFERRED"
assert policy["activation"] == "compile-time-opt-in-session-required"
assert len(policy["requested_pilot_operations"]) == 4
assert all("rust-transaction" in operation for operation in policy["requested_pilot_operations"])
assert policy["threat_model"]["native_linux_elf_injection"] == "excluded"
assert "non-evicting" in policy["transaction_identity"]
assert "durable-wal" in policy["transaction_identity"]
assert "private-quarantine" in policy["transaction_durability"]
assert "gc-pending-gc-done" in policy["durable_gc"]
assert "ftruncate-fsync" in policy["torn_wal_policy"]
assert "recovery-pending" in policy["commit_failure_policy"]
assert "msg-cmsg-cloexec" in policy["created_fd_delivery"]
assert "guest-kernel-umask" in policy["mode_semantics"]
assert "exact-running-darlingserver" in policy["lower_authority"]
assert "quarantine" in policy["leaf_authority"]
assert 'option(DARLING_LIFECYCLE_COHORT_V1 "Build the opt-in Rust lifecycle endpoint cohort" OFF)' in (
    TASK / "darling/CMakeLists.txt"
).read_text()

required = {
    TASK / "darling/src/startup/mldr/mldr.c": ["recvmsg(", "SCM_RIGHTS", "guest_namespace_capabilities"],
    TASK / "xnu/darling/src/libsystem_kernel/emulation/src/other/mach/lkm.c": [
        "darling_guest_namespace_initialize", "guard_flag_prevent_close"
    ],
    TASK / "xnu/darling/src/libsystem_kernel/emulation/src/linux_premigration/guest_namespace_authority.c": [
        "dserver_rpc_guest_namespace_transaction", "transaction_rpc", "mutation_still_valid",
        "__NR_getrandom", "guest_effective_mode", "F_SETFD", "FD_CLOEXEC"
    ],
    TASK / "xnu/darling/src/libsystem_kernel/emulation/include/linux_premigration/resources/dserver-rpc-defs.h": [
        "LINUX_MSG_CMSG_CLOEXEC", "LINUX_MSG_DONTWAIT | LINUX_MSG_CMSG_CLOEXEC"
    ],
    ROOT / "lifecycle/operation-boundary/src/guest_namespace_transaction.rs": [
        "GuestNamespaceTransactionService", "duplicate_created_result", "RecoveryObligation",
        "LayerState::Whiteout", "transaction budget exhausted", "AfterQuarantineVerify",
        "crash_after_private_quarantine_move_recovers_exact_authority_from_wal",
        "effective_guest_mode_is_exact_despite_controller_umask",
        "committed_unlink_restores_gc_pending_and_persists_gc_done",
        "committed_create_restart_replays_with_exact_retained_fd",
        "torn_tail_is_physically_repaired_before_new_commit_and_second_restart",
        "create_commit_write_and_fsync_failures_retain_post_mutation_authority",
        "unlink_commit_write_and_fsync_failures_retain_quarantine_authority",
        "rename_commit_write_and_fsync_failures_retain_published_authority",
    ],
}
for path, markers in required.items():
    text = path.read_text()
    for marker in markers:
        assert marker in text, (path, marker)

dserver = (TASK / "darlingserver/src/darlingserver.cpp").read_text()
activation_block = dserver[dserver.index("const bool lifecycleCohortEnabled"):][:300]
assert "= true" in activation_block
assert "getenv" not in activation_block
mldr = (TASK / "darling/src/startup/mldr/mldr.c").read_text()
assert "Required Rust guest namespace bootstrap is missing" in mldr

for relative in (
    "stat/mkdirat.c", "fcntl/openat.c", "unistd/unlinkat.c", "unistd/renameat.c"
):
    candidates = list((TASK / "xnu/darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl").rglob(relative))
    assert len(candidates) == 1
    text = candidates[0].read_text()
    assert "DARLING_GUEST_NAMESPACE_REQUIRED" in text
    assert "ENOTSUP" in text

generator = (TASK / "darlingserver/scripts/generate-rpc-wrappers.py").read_text()
assert "guest_namespace_transaction" in generator
assert "created_fd" in generator
assert "('flags', 'int32_t')" in generator
assert "DARLING_LIFECYCLE_FINISH_RECOVERY_PENDING" in dserver

fixture = ROOT / "tests/fixtures/guest_namespace_authority_pilot.c"
with tempfile.TemporaryDirectory(prefix="dar-4ush.7.3.3-contract-") as temp:
    binary = Path(temp) / "pilot"
    subprocess.run([
        "cc", "-std=gnu11", "-Wall", "-Wextra", "-Werror", "-pthread",
        "-I", str(TASK / "xnu/darling/src/libsystem_kernel/emulation/include/linux_premigration"),
        str(fixture), "-o", str(binary),
    ], check=True)
    subprocess.run([str(binary)], check=True, env={})

    owner_binary = Path(temp) / "cohort-owner-drain"
    subprocess.run([
        "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
        "-I", str(TASK / "darlingserver/internal-include"),
        "-I", str(ROOT / "lifecycle/operation-boundary/include"),
        str(ROOT / "tests/fixtures/lifecycle-cohort-v1/cohort_owner_drain.cpp"),
        "-o", str(owner_binary),
    ], check=True)
    subprocess.run([str(owner_binary)], check=True)
    subprocess.run([
        "cargo", "test", "--manifest-path",
        str(ROOT / "lifecycle/operation-boundary/Cargo.toml"),
        "--lib", "guest_namespace_transaction", "--", "--nocapture",
    ], check=True, env={
        **os.environ,
        "CARGO_TARGET_DIR": str(Path(temp) / "cargo-target"),
    })
    subprocess.run([
        "cargo", "test", "--manifest-path",
        str(ROOT / "lifecycle/operation-boundary/Cargo.toml"),
        "--test", "cohort_child_forensic", "--", "--nocapture",
    ], check=True, env={
        **os.environ,
        "CARGO_TARGET_DIR": str(Path(temp) / "cargo-target"),
    })
    forensic = Path(temp) / "persistent-forensic-endpoint"
    persistent = subprocess.run(
        [str(owner_binary), str(forensic)],
        check=False,
        preexec_fn=lambda: resource.setrlimit(resource.RLIMIT_CORE, (0, 0)),
    )
    assert persistent.returncode < 0
    assert stat.S_ISSOCK(forensic.lstat().st_mode)

print("GUEST_NAMESPACE_AUTHORITY_PILOT_VALID")
