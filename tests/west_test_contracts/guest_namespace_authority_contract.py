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
assert all("blocked-with-enotsup" in operation for operation in policy["requested_pilot_operations"])
assert policy["threat_model"]["native_linux_elf_injection"] == "excluded"
assert 'option(DARLING_LIFECYCLE_COHORT_V1 "Build the opt-in Rust lifecycle endpoint cohort" OFF)' in (
    TASK / "darling/CMakeLists.txt"
).read_text()

required = {
    TASK / "darling/src/startup/mldr/mldr.c": ["recvmsg(", "SCM_RIGHTS", "guest_namespace_capabilities"],
    TASK / "xnu/darling/src/libsystem_kernel/emulation/src/other/mach/lkm.c": [
        "darling_guest_namespace_initialize", "guard_flag_prevent_close"
    ],
    TASK / "xnu/darling/src/libsystem_kernel/emulation/src/linux_premigration/guest_namespace_authority.c": [
        "CAP_OPENAT", "CAP_MKDIRAT", "CAP_UNLINKAT", "CAP_RENAMEAT", "/proc/self/fd/"
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

fixture = ROOT / "tests/fixtures/guest_namespace_authority_pilot.c"
with tempfile.TemporaryDirectory(prefix="dar-4ush.7.3.2-contract-") as temp:
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
