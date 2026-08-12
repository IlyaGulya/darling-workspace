"""Architecture contract for the Rust-owned dar-4ush.7 controller gate."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path

from jsonschema import Draft202012Validator

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from west_commands.lifecycle_controller_transport import (  # noqa: E402
    LifecycleControllerTransport,
    LifecycleControllerTransportError,
)


ROOT = Path(__file__).resolve().parents[2]
CRATE = ROOT / "lifecycle" / "operation-boundary"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "rootless-controller-v1"


def load(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise AssertionError(f"fixture is not an object: {path}")
    return value


request_schema = load(ROOT / "schemas" / "rootless-lifecycle-controller-request-v1.schema.json")
response_schema = load(ROOT / "schemas" / "rootless-lifecycle-controller-response-v1.schema.json")
architecture = load(ROOT / "lifecycle" / "rootless-controller-v1.json")
assert request_schema["$id"].endswith("request-v1.schema.json")
assert response_schema["$id"].endswith("response-v1.schema.json")
assert request_schema["additionalProperties"] is False
assert response_schema["additionalProperties"] is False
Draft202012Validator.check_schema(request_schema)
Draft202012Validator.check_schema(response_schema)
request_validator = Draft202012Validator(request_schema)
valid_request = {
    "schema_version": 1,
    "transaction_id": "architecture-contract",
    "profile": "rootless",
    "operation": "REQUEST_SHUTDOWN",
    "anchor_fd": 3,
    "evidence_fd": None,
    "controller_closure_sha256": "0" * 64,
    "runtime_identity_digest": "2" * 64,
    "request_nonce": "3" * 64,
    "budget": {
        "max_events": 16,
        "max_virtual_time_ns": 1000,
        "max_members": 4,
        "max_recovery_steps": 4,
        "deadline_ns": 1000,
    },
}
assert request_validator.is_valid(valid_request)
unknown = dict(valid_request, forged_kernel_fact=True)
assert not request_validator.is_valid(unknown)
oversized = dict(valid_request, transaction_id="x" * 129)
assert not request_validator.is_valid(oversized)
wrong_profile = dict(valid_request, profile="perf")
assert not request_validator.is_valid(wrong_profile)
negative_fd = dict(valid_request, anchor_fd=-1)
assert not request_validator.is_valid(negative_fd)
assert architecture["authority"] == "rust-controller"
assert architecture["production_routing"] == "deferred"
assert architecture["acquisition"] == "rust-fd-relative-from-anchor"
assert architecture["partial_transition"] == "recovery-pending-retains-capabilities-and-journal-then-terminal-fail-closed"
assert architecture["handshake"]["response_schema"] == "draft-2020-12-closed"
assert architecture["fixtures"] == {"count": 13, "execution": "rust-typed-fault-backend", "all_expected_classes_are_run": True}
assert architecture["writer_protocol"] == {
    "threat_model": "cooperative-writers",
    "lease": "retained-exclusive-flock",
    "hostile_same_uid_writer": "out-of-scope",
    "routing_gate": "inventory-all-product-writers-and-prove-lease-before-mutation",
    "first_cohort": {
        "status": "opt-in-cohort-ready-global-route-disabled",
        "controller": "darlingserver-host-rust-staticlib",
        "lifetime": "pidfd-watched-supervisor-cleans-after-explicit-finish-or-owner-sigkill",
        "authority": "retained-prefix-fd-and-one-exact-session-flock",
        "lease_order": "existing-lock-validated-then-flocked-before-mutation-new-lock-normalized-only-after-flock",
        "transport": "bounded-per-connection-seqpacket-nonce-peercred-retained-peerpidfd-direct-parent-scm-rights-invalid-input-never-terminates-authority",
        "owner_death": "retained-pidfd-gone-exact-inode-retire-then-keepalive-republish",
        "post_publication_failure": "close-received-capability-and-exact-rust-retire",
        "nonce_exposure": "launchd-and-authorized-shellspawn-only-not-exportable-user-environment",
        "writers": [
            "rust-controller.transport-socket",
            "darling.startup.init-pid",
            "darling.startup.shellspawn-preflight",
            "darling.shellspawn.socket",
            "darlingserver.control-socket",
            "launchd.system-ipc-socket",
        ],
    },
}
assert architecture["phases"] == [
    "Prepared",
    "Acquired",
    "MembershipBound",
    "ShutdownRequested",
    "Drained",
    "Quiescent",
    "Cleaned",
    "Finalized",
]

transport = (ROOT / "west_commands" / "lifecycle_controller_transport.py").read_text()
for forbidden in ("pidfd_open", "os.kill(", "unlink(", "rmtree(", "MATCH", "GONE"):
    assert forbidden not in transport, f"transport owns lifecycle authority: {forbidden}"
assert "/proc/self/fd/" in transport
assert all(token in transport for token in ("Draft202012Validator", "start_new_session", "pass_fds", "expected_fds", "total_output", "request_nonce", "duplicate descriptor"))
assert "phase_order" not in transport and "recovery_obligations" not in transport and "terminal_indices" not in transport
assert "MAX_TRANSPORT_INPUT_BYTES" in transport and "MAX_TRANSPORT_OUTPUT_BYTES" in transport
assert "os.killpg" in transport and "process.wait(timeout=0.25)" in transport

controller = (CRATE / "src" / "controller.rs").read_text()
quarantine_gc = (CRATE / "src" / "quarantine_gc.rs").read_text()
boundary = (CRATE / "src" / "lib.rs").read_text()
linux_backend = (CRATE / "src" / "linux_backend.rs").read_text()
rootless_producer = (ROOT / "west_commands" / "rootless_shutdown_lifecycle.py").read_text()
build_script = (CRATE / "build.rs").read_text()
assert "CONTROLLER_CLOSURE" in build_script
assert "LIFECYCLE_CONTROLLER_CLOSURE_SHA256" in build_script
assert "src/controller.rs" in build_script
assert "src/linux_backend.rs" in build_script
assert "src/quarantine_gc.rs" in build_script
assert "run-lifecycle-linux-backend-contract.sh" in build_script
assert "run-lifecycle-quarantine-gc-contract.sh" in build_script
assert "lifecycle-controller-quarantine-gc-v1.md" in build_script
assert "fixtures/rootless-controller-v1/ancestor-swap.json" in build_script
assert "fixtures/rootless-controller-v1/stale-controller.json" in build_script
for phase in architecture["phases"]:
    assert f"pub struct {phase}" in controller or f"pub struct {phase} " in controller
for capability in architecture["capabilities"][:4]:
    assert f"pub struct {capability}" in controller
assert "pub(crate) trait SignalExecutor" in controller
assert "pub(crate) trait EndpointCleanupExecutor" in controller
assert "pub(crate) trait CapabilityInspector" in controller
assert "mod sealed" in controller
assert "pub unsafe fn from_external" not in controller
assert "launcher_fd" not in controller
assert "from_owned(" not in controller
assert "RecoveryPending" in controller and "ControllerVerdict::Success" in controller
assert "finalize_fail_closed" in controller
assert "QuarantinePending" in controller and "try_into_quarantine_pending" in controller
assert "mod sealed" in quarantine_gc
assert "impl QuarantineGcAuthority" in quarantine_gc
assert "    fn new(" in quarantine_gc
assert "flock" in quarantine_gc and "WriterIdentityMismatch" in quarantine_gc
assert "QuarantinePending" in quarantine_gc and "into_parts" in quarantine_gc
assert "ControllerQuiescenceGrant" in quarantine_gc
assert "issue_quarantine_gc_grant" in controller
assert "from_quiescent" in quarantine_gc
assert "red_quarantine_handoff_uses_controller_issuer" in controller
assert "BeforeEndpointDelete" in quarantine_gc and "BeforePlaceholderDelete" in quarantine_gc
assert "authority.revoke" not in quarantine_gc and "valid: bool" not in quarantine_gc
assert "cooperative_writer_protocol_blocks_unleased_mutation" in quarantine_gc
assert "AfterFinalEndpointVerify" not in quarantine_gc
assert "AfterFinalPlaceholderVerify" not in quarantine_gc
assert "pub fn mkdir_start(parent: &DirCap, lease: &ExclusiveLease)" in boundary
assert "pub fn unlink(parent: &DirCap, lease: &ExclusiveLease)" in boundary
assert "lease: &ExclusiveLease" in boundary
assert "acquire_exclusive(lock_fd.as_raw_fd()" in linux_backend
assert "self.revalidate_lease()" in linux_backend
assert "rename_exchange" in linux_backend and "rename_noreplace" in linux_backend
assert all(token not in rootless_producer for token in ("unlink(", "rename(", "mkdir(", "rmtree("))
assert "finalize_terminal" not in controller and "RecoveryTerminal" not in controller
assert "BudgetLedger" in controller and ".event()" in controller
assert "from_inherited" in controller and "fn acquire_from_anchor" in controller and "fn cleanup_fd_relative" in controller
assert "ProductProtocol" in controller
assert "SignalResult::Deadline" in controller and "SignalResult::Rejected" in controller
assert "returncode" not in controller
for capability in ("PrefixCapability", "MarkerCapability", "LauncherCapability", "PidFdCapability"):
    assert f"impl Clone for {capability}" not in controller
assert "pub mod linux_backend;" in (ROOT / "lifecycle" / "operation-boundary" / "src" / "lib.rs").read_text()

valid_response = {
    "schema_version": 1,
    "transaction_id": "architecture-contract",
    "controller_closure_sha256": "0" * 64,
    "runtime_identity_digest": "2" * 64,
    "request_nonce": "3" * 64,
    "verdict": "SUCCESS",
    "obligations": [],
    "signals": [],
    "journal": [
        {"kind": "PREPARED"},
        {"kind": "CAPABILITIES_ACQUIRED"},
        {"kind": "MEMBERSHIP_BOUND", "member_count": 0},
        {"kind": "SHUTDOWN_REQUESTED", "signals": []},
        {"kind": "DRAINED"},
        {"kind": "QUIESCENT"},
        {"kind": "CLEANED", "endpoint_count": 0},
        {"kind": "FINALIZED", "verdict": "SUCCESS"},
    ],
}
response_validator = Draft202012Validator(response_schema)
assert response_validator.is_valid(valid_response)
partial_signal_fail_closed = dict(
    valid_response,
    verdict="FAIL_CLOSED",
    obligations=["SIGNAL_EVIDENCE"],
    signals=[{"kind": "RUST_PIDFD", "target": "SESSION_MEMBER", "signal": "KILL", "result": "SENT"}],
    journal=[
        {"kind": "PREPARED"},
        {"kind": "CAPABILITIES_ACQUIRED"},
        {"kind": "MEMBERSHIP_BOUND", "member_count": 1},
        {"kind": "RECOVERY", "obligation": "SIGNAL_EVIDENCE", "completed": 1},
        {"kind": "FINALIZED", "verdict": "FAIL_CLOSED"},
    ],
)
assert response_validator.is_valid(partial_signal_fail_closed)
for forged in (
    dict(valid_response, verdict="RECOVERED", obligations=[], journal=[{"kind": "PREPARED"}, {"kind": "FINALIZED", "verdict": "RECOVERED"}]),
    dict(valid_response, journal=[{"kind": "PREPARED"}, {"kind": "FINALIZED", "verdict": "PREPARED"}]),
    dict(valid_response, journal=[{"kind": "PREPARED"}, {"kind": "FINALIZED", "verdict": "SUCCESS", "signals": []}]),
):
    assert not response_validator.is_valid(forged)

adapter = LifecycleControllerTransport(
    binary=Path("/dev/null"),
    response_schema=ROOT / "schemas" / "rootless-lifecycle-controller-response-v1.schema.json",
    expected_controller_closure_sha256="0" * 64,
    expected_runtime_identity_digest="2" * 64,
    request_nonce="3" * 64,
)
assert adapter._validate_response(valid_request, valid_response) == valid_response
assert adapter._validate_response(valid_request, partial_signal_fail_closed) == partial_signal_fail_closed
for forged in (
    dict(valid_response, journal=[{"kind": "FINALIZED", "verdict": "SUCCESS"}, {"kind": "PREPARED"}]),
    dict(valid_response, verdict="RECOVERED", obligations=["SIGNAL_EVIDENCE"], journal=[{"kind": "PREPARED"}, {"kind": "FINALIZED", "verdict": "RECOVERED"}]),
    dict(valid_response, journal=[{"kind": "PREPARED"}, {"kind": "FINALIZED", "verdict": "SUCCESS"}]),
    dict(valid_response, verdict="FAIL_CLOSED", obligations=["BUDGET_EXCEEDED"], journal=[{"kind": "PREPARED"}, {"kind": "FINALIZED", "verdict": "FAIL_CLOSED"}]),
):
    try:
        adapter._validate_response(valid_request, forged)
    except LifecycleControllerTransportError:
        pass
    else:
        raise AssertionError("transport accepted a response rejected by the closed schema")

expected = {
    "marker-in-place": "MARKER_IDENTITY",
    "membership-drift-provisioning": "MEMBERSHIP_CHANGED",
    "membership-drift-before-shutdown": "MEMBERSHIP_CHANGED",
    "endpoint-replacement-after-check": "ENDPOINT_REPLACEMENT",
    "deployed-launcher-replacement": "ENDPOINT_REPLACEMENT",
    "ancestor-swap": "MEMBERSHIP_CHANGED",
    "late-fork": "LATE_FORK",
    "pid-reuse": "PID_REUSE",
    "member-gone": "MEMBERSHIP_CHANGED",
    "malformed-transport": "MALFORMED_REQUEST",
    "deadline": "BUDGET_EXCEEDED",
    "sigint": "CONTROLLER_INTERRUPTED",
    "stale-controller": "MALFORMED_REQUEST",
}
fixtures = sorted(FIXTURE_ROOT.glob("*.json"))
assert {load(path)["id"] for path in fixtures} == set(expected)
for path in fixtures:
    fixture = load(path)
    assert fixture["expected"] == expected[fixture["id"]]
    assert isinstance(fixture.get("mutation"), dict) and fixture["mutation"]
    assert fixture["mutation"].get("kind") == fixture["id"]


def assert_process_group_unwind_fixture() -> None:
    """Exercise transport timeout cleanup with a descendant-owned pipe.

    The controller parent exits immediately, while its forked child keeps the
    stdout pipe open.  A bounded transport timeout must kill the whole owned
    process group and wait until the descendant is gone; killing only the
    immediate Popen child would leave the selector blocked and the PID alive.
    """

    with tempfile.TemporaryDirectory(prefix="dar-4ush-7-transport-") as owned_root:
        root = Path(owned_root)
        script = root / "controller-parent.py"
        pid_file = root / "descendant.pid"
        group_file = root / "controller.pgid"
        script.write_text(
            "#!/usr/bin/python3\n"
            "import os\n"
            "import time\n"
            "pid_file = os.environ['LIFECYCLE_DESCENDANT_PID_FILE']\n"
            "group_file = os.environ['LIFECYCLE_CONTROLLER_GROUP_FILE']\n"
            "with open(group_file, 'w', encoding='ascii') as stream: stream.write(str(os.getpgrp()))\n"
            "child = os.fork()\n"
            "if child:\n"
            "    with open(pid_file, 'w', encoding='ascii') as stream: stream.write(str(child))\n"
            "    os._exit(0)\n"
            "while True:\n"
            "    time.sleep(60)\n"
        )
        script.chmod(0o700)
        anchor_fd = os.open(owned_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        group_pid: int | None = None
        descendant_pid: int | None = None
        old_pid_file = os.environ.get("LIFECYCLE_DESCENDANT_PID_FILE")
        old_group_file = os.environ.get("LIFECYCLE_CONTROLLER_GROUP_FILE")
        os.environ["LIFECYCLE_DESCENDANT_PID_FILE"] = str(pid_file)
        os.environ["LIFECYCLE_CONTROLLER_GROUP_FILE"] = str(group_file)
        try:
            request = dict(valid_request, anchor_fd=anchor_fd)
            adapter = LifecycleControllerTransport(
                binary=script,
                response_schema=ROOT / "schemas" / "rootless-lifecycle-controller-response-v1.schema.json",
                expected_controller_closure_sha256="0" * 64,
                expected_runtime_identity_digest="2" * 64,
                request_nonce="3" * 64,
                timeout_seconds=0.25,
            )
            try:
                adapter.invoke(request, inherited_fds=(anchor_fd,))
            except LifecycleControllerTransportError as error:
                assert "deadline" in str(error) or "terminate" in str(error)
            else:
                raise AssertionError("transport accepted a parent with a pipe-holding descendant")

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                if pid_file.exists():
                    descendant_pid = int(pid_file.read_text())
                if group_file.exists():
                    group_pid = int(group_file.read_text())
                if descendant_pid is not None:
                    if not Path(f"/proc/{descendant_pid}").exists():
                        break
                    try:
                        os.kill(descendant_pid, 0)
                    except ProcessLookupError:
                        break
                time.sleep(0.01)
            assert descendant_pid is not None, "fixture did not publish descendant identity"
            assert not Path(f"/proc/{descendant_pid}").exists(), "transport left a descendant PID entry"
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("transport left a descendant process alive")
        finally:
            # This is only a last-resort cleanup for a failed assertion.  The
            # transport itself must perform the normal group unwind.
            if group_pid is not None:
                try:
                    os.killpg(group_pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            os.close(anchor_fd)
            if old_pid_file is None:
                os.environ.pop("LIFECYCLE_DESCENDANT_PID_FILE", None)
            else:
                os.environ["LIFECYCLE_DESCENDANT_PID_FILE"] = old_pid_file
            if old_group_file is None:
                os.environ.pop("LIFECYCLE_CONTROLLER_GROUP_FILE", None)
            else:
                os.environ["LIFECYCLE_CONTROLLER_GROUP_FILE"] = old_group_file


assert_process_group_unwind_fixture()

fixture_digest = hashlib.sha256(
    "\n".join(path.name + ":" + hashlib.sha256(path.read_bytes()).hexdigest() for path in fixtures).encode()
).hexdigest()
print(
    "LIFECYCLE_CONTROLLER_ARCHITECTURE_VALID "
    f"fixtures={len(fixtures)} fixture_digest={fixture_digest} "
    "authority=rust phases=8 routing=deferred"
)
