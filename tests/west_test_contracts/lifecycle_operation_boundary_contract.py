"""Contract for the Rust lifecycle boundary and its thin West adapter."""

from __future__ import annotations

import ast
import copy
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.lifecycle_operation_boundary import (  # noqa: E402
    RustBoundaryAdapter,
    RustBoundaryUnavailable,
)
from west_commands.lifecycle_state_model import (  # noqa: E402
    load_json,
    replay_trace,
)


policy = json.loads((ROOT / "lifecycle" / "operation-boundary-v1.json").read_text(encoding="utf-8"))
assert policy["schema_version"] == 1
assert policy["kind"] == "lifecycle-operation-boundary-policy"
assert policy["backend"]["production"] == "rust"
assert policy["backend"]["test"] == "same-rust-facade-with-scripted-clock-observer-faults"
assert policy["operations"][-1] == "quarantine"
assert policy["acceptance_scope"] == {
    "boundary": "infrastructure-only",
    "production_routing": "deferred_to_dar-4ush.7",
    "optimized_overhead": "deferred_to_dar-4ush.7",
}
assert policy["capability_kinds"] == ["directory", "file", "pidfd", "lock", "exclusive_lease"]
assert policy["rules"]["rust_owns_syscalls"] is True
assert policy["rules"]["python_owns_syscalls"] is False
assert policy["rules"]["mutation_requires_exclusive_lease"] is True
assert policy["rules"]["capability_scope_branded"] is True
assert policy["rules"]["cleanup_uses_quarantine_move"] is True
assert policy["rules"]["quarantine_gc_requires_quiescence"] is True
assert policy["rules"]["quarantine_gc_requires_external_scope"] is True
assert policy["rules"]["quarantine_obligation_ownership"] is True
assert policy["rules"]["stage_obligation_ownership"] is True
assert policy["rules"]["obligation_queues_are_disjoint"] is True
assert policy["rules"]["typed_journal_records"] is True
assert policy["rules"]["operation_specific_capability_constructors"] is True
assert policy["rules"]["stage_terminal_record"] is True
assert policy["rules"]["stage_cleanup_terminal_record"] is True

rust_source = (ROOT / "lifecycle" / "operation-boundary" / "src" / "lib.rs").read_text(encoding="utf-8")
assert "capability_type!(ExclusiveLease" in rust_source
assert "pub fn flock_exclusive" in rust_source
assert "pub fn flock(" not in rust_source
assert "lease: &ExclusiveLease" in rust_source
assert "pub fn write" in rust_source
assert "AfterMoveBeforeVerify" in rust_source
assert "MutatedError" in rust_source
assert "RollbackIncomplete" in rust_source
assert "StageRegistered" in rust_source
assert "StageCleanup" in rust_source
assert "AfterStageMkdirBeforeBind" in rust_source
assert "flock_exclusive" in rust_source
assert "AfterQuarantineMoveBeforeVerify" in rust_source
assert "AfterQuarantineVerifyBeforeGc" in rust_source
assert "lifecycle-quarantine-" in rust_source
assert "authority scope" in rust_source
assert "cleanup_registered" in rust_source
assert "pub struct QuiescentScope" in rust_source
assert "pub struct QuarantinedObject" in rust_source
assert "from_external" in rust_source
assert "acquire_quiescence" not in rust_source
assert "take_quarantine_obligations" in rust_source
assert "take_stage_obligations" in rust_source
assert "pub fn into_parts" in rust_source
assert "pub fn finish" in rust_source
assert "pub fn into_observer(self) -> std::result::Result<O, BoundaryFinishError<O>>" in rust_source
assert "stage_obligations" in rust_source
assert "QuarantineObligation" in rust_source
assert "UnboundQuarantine" in rust_source
assert "pub enum OperationOutcome" in rust_source
assert "Observed" in rust_source
assert "Applied" in rust_source
assert "    Ok," not in rust_source
assert "pub enum MutationState" in rust_source
assert "pub enum IdentityObservation" in rust_source
assert "pub enum CapabilityRole" in rust_source
assert "pub struct CapabilitySet" in rust_source
assert "pub enum StageJournal" in rust_source
assert "pub enum StageObligation" in rust_source
assert "pub struct StageObligations" in rust_source
assert "pub struct BoundStageObligation" in rust_source
assert "retain_stage_authority" in rust_source
assert "RollbackDisposition" in rust_source
assert "pub enum QuarantineEvent" in rust_source
assert "pub struct OperationEvent" in rust_source
assert "pub enum JournalRecord" in rust_source
assert "pub capabilities: CapabilitySet" in rust_source
assert "pub result: OperationOutcome" in rust_source
assert "pub outcome: OperationOutcome" not in rust_source
assert "pub mutation: MutationState" not in rust_source
assert "pub record: JournalRecord" in rust_source
assert "pub fn write(file: &FileCap, lease: &ExclusiveLease)" in rust_source
assert "pub fn mkdir_child(" in rust_source
assert "pub fn rename(" in rust_source
assert "pub fn publish(" in rust_source
assert "StagePublished" in rust_source
assert "fn for_operation" not in rust_source
assert "impl From<&'static str> for OperationOutcome" not in rust_source
assert "fn from_legacy" not in rust_source
assert "fn record_with_identity" not in rust_source
assert "mutated: bool" not in rust_source
assert "pub identity: IdentityObservation" not in rust_source
for legacy_outcome in (
    "ok",
    "error",
    "injected",
    "allocated",
    "registered",
    "deferred",
    "staged-error",
    "rolled-back-error",
    "rollback-incomplete",
    "identity-mismatch",
    "mutated-error",
):
    assert f'"{legacy_outcome}"' not in rust_source
assert "journal_capability_roles_are_typed_per_operation" in rust_source
record_block = rust_source[
    rust_source.index("pub struct OperationRecord") : rust_source.index("pub trait Observer")
]
assert "pub result: &'static str" not in record_block
assert "pub capability_ids: Vec<u64>" not in record_block
assert "pub identity: Option<FileIdentity>" not in record_block
assert "pub payload:" not in record_block
assert "gc_quarantine" in rust_source
assert "QuarantineRequired" in rust_source
assert "struct ScopeId" in rust_source
assert "static NEXT_SCOPE_ID" in rust_source
assert "pub struct NoopObserver" in rust_source
assert "Boundary<RealClock, NoopObserver, NoFault>" in rust_source


# Python is a JSON subprocess adapter only.  This static check prevents a
# future convenience edit from reintroducing lifecycle syscalls or fd policy.
adapter_path = ROOT / "west_commands" / "lifecycle_operation_boundary.py"
tree = ast.parse(adapter_path.read_text(encoding="utf-8"), filename=str(adapter_path))
for node in ast.walk(tree):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
        assert not (
            node.func.value.id in {"os", "fcntl", "signal", "time"}
            and node.func.attr in {"open", "mkdir", "unlink", "rename", "fstat", "flock", "kill", "sleep"}
        ), f"Python adapter contains lifecycle syscall {node.func.value.id}.{node.func.attr}"
assert "subprocess.Popen" in adapter_path.read_text(encoding="utf-8")
assert "_TRANSPORT_TIMEOUT_SECONDS" in adapter_path.read_text(encoding="utf-8")
assert "_TRANSPORT_INPUT_LIMIT" in adapter_path.read_text(encoding="utf-8")
assert "selector.select(remaining)" in adapter_path.read_text(encoding="utf-8")


binary = Path(os.environ.get("DARLING_LIFECYCLE_BOUNDARY_BIN", ROOT / "lifecycle" / "operation-boundary" / "target" / "debug" / "lifecycle-boundary"))
adapter = RustBoundaryAdapter(ROOT, binary)
try:
    response = adapter.invoke({"op": "self_check", "policy": policy})
except RustBoundaryUnavailable as error:
    raise AssertionError(str(error)) from error
assert response == {"backend": "rust", "op": "self_check", "status": "ok"}

tampered_policy = copy.deepcopy(policy)
tampered_policy["backend"]["production"] = "python"
try:
    adapter.invoke({"op": "self_check", "policy": tampered_policy})
except RuntimeError:
    pass
else:
    raise AssertionError("Rust policy validator accepted a non-Rust production backend")

tampered_policy = copy.deepcopy(policy)
tampered_policy["unexpected"] = True
try:
    adapter.invoke({"op": "self_check", "policy": tampered_policy})
except RuntimeError:
    pass
else:
    raise AssertionError("Rust policy validator accepted an unexpected top-level field")

tampered_policy = copy.deepcopy(policy)
tampered_policy["rules"]["future_rule"] = True
try:
    adapter.invoke({"op": "self_check", "policy": tampered_policy})
except RuntimeError:
    pass
else:
    raise AssertionError("Rust policy validator accepted an unexpected rule")

with tempfile.TemporaryDirectory(prefix="lifecycle-boundary-transport-") as temporary:
    noisy = Path(temporary) / "noisy-boundary"
    noisy.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('x' * 65537)\n",
        encoding="utf-8",
    )
    noisy.chmod(0o700)
    try:
        RustBoundaryAdapter(ROOT, noisy).invoke({"op": "self_check", "policy": policy})
    except RuntimeError as error:
        assert "64 KiB" in str(error)
    else:
        raise AssertionError("transport accepted output beyond its hard bound")

with tempfile.TemporaryDirectory(prefix="lifecycle-boundary-input-") as temporary:
    no_read = Path(temporary) / "no-read-boundary"
    no_read.write_text(
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(10)\n",
        encoding="utf-8",
    )
    no_read.chmod(0o700)
    bounded = RustBoundaryAdapter(ROOT, no_read, timeout_seconds=0.1)
    try:
        bounded.invoke(
            {"op": "self_check", "policy": policy, "padding": "x" * 60_000}
        )
    except RustBoundaryUnavailable as error:
        assert "transport deadline" in str(error)
    else:
        raise AssertionError("transport did not bound a child that never reads stdin")

    try:
        RustBoundaryAdapter(ROOT, no_read).invoke(
            {"op": "self_check", "policy": policy, "padding": "x" * 70_000}
        )
    except RuntimeError as error:
        assert "input exceeds" in str(error)
    else:
        raise AssertionError("transport accepted input beyond its hard bound")


trace_root = ROOT / "tests" / "fixtures" / "lifecycle-traces" / "v1"
for trace_path in sorted(trace_root.glob("*.json")):
    document = load_json(trace_path)
    python_result = replay_trace(document)
    rust_result = adapter.invoke({"op": "replay_trace", "trace": document})
    expected_result = {
        "trace_id": python_result.trace_id,
        "outcome": python_result.outcome,
        "final_snapshot": {
            "stable_state": python_result.final_snapshot.stable_state.value,
            "journal_phase": python_result.final_snapshot.journal_phase.value,
            "intent": python_result.final_snapshot.intent.value,
        },
        "recovery_observations": [
            {
                "stable_state": stable,
                "journal_phase": journal,
                "action": action,
            }
            for stable, journal, action in python_result.recovery_observations
        ],
        "live_capabilities": [
            {"capability": capability, "owner": owner}
            for capability, owner in python_result.live_capabilities
        ],
        "obligations": list(python_result.obligations),
        "satisfied_invariants": list(python_result.satisfied_invariants),
    }
    assert rust_result == expected_result, f"Rust/Python differential mismatch for {trace_path.name}"


def assert_both_reject(document: dict, label: str) -> None:
    try:
        replay_trace(document)
    except Exception:
        pass
    else:
        raise AssertionError(f"Python accepted {label}")
    try:
        adapter.invoke({"op": "replay_trace", "trace": document})
    except RuntimeError:
        pass
    else:
        raise AssertionError(f"Rust accepted {label}")


gone_trace = load_json(trace_root / "shared-session-signal-gone.json")
for label, data in {
    "membership-after-gone": {
        "seq": 0,
        "time_ns": 6000,
        "actor": "observer",
        "kind": "membership_snapshot",
        "data": {
            "authority": "SESSION_LEDGER",
            "members": ["cap.session-root"],
            "completeness": "CLOSED",
        },
        "state_after": gone_trace["events"][-1]["state_after"],
    },
    "signal-after-gone": {
        "seq": 0,
        "time_ns": 6000,
        "actor": "controller",
        "kind": "signal_sent",
        "data": {"capability": "cap.session-root", "signal": "TERM", "result": "SENT"},
        "state_after": gone_trace["events"][-1]["state_after"],
    },
}.items():
    mutated = copy.deepcopy(gone_trace)
    terminal = mutated["events"].pop()
    mutated["events"].append(data)
    terminal["seq"] = len(mutated["events"])
    mutated["events"].append(terminal)
    for index, event in enumerate(mutated["events"]):
        event["seq"] = index
    assert_both_reject(mutated, label)

rejected_trace = load_json(trace_root / "shared-session-signal-rejected.json")
unrecovered_observed = copy.deepcopy(rejected_trace)
unrecovered_observed["events"].pop(-2)  # retain RECOVERED with signal-failure unresolved
unrecovered_observed["recovery_observations"] = []
for index, event in enumerate(unrecovered_observed["events"]):
    event["seq"] = index
python_unrecovered = replay_trace(unrecovered_observed)
assert "signal-failure" in python_unrecovered.obligations
rust_unrecovered = adapter.invoke({"op": "replay_trace", "trace": unrecovered_observed})
assert "signal-failure" in rust_unrecovered["obligations"]

unrecovered_rejected = copy.deepcopy(rejected_trace)
unrecovered_rejected["events"].pop(-2)  # remove the ContinueDrain recovery
unrecovered_rejected["events"][-1]["data"]["outcome"] = "SUCCESS"
unrecovered_rejected["expected"]["outcome"] = "SUCCESS"
for index, event in enumerate(unrecovered_rejected["events"]):
    event["seq"] = index
assert_both_reject(unrecovered_rejected, "rejected-signal-without-recovery")

tampered_trace = copy.deepcopy(load_json(sorted(trace_root.glob("*.json"))[0]))
tampered_trace["events"][0]["unexpected"] = True
try:
    adapter.invoke({"op": "replay_trace", "trace": tampered_trace})
except RuntimeError:
    pass
else:
    raise AssertionError("Rust trace ingress accepted an unexpected event field")

tampered_trace = copy.deepcopy(load_json(sorted(trace_root.glob("*.json"))[0]))
if not tampered_trace["recovery_observations"]:
    raise AssertionError("golden trace unexpectedly has no recovery observation")
tampered_trace["recovery_observations"] = []
try:
    adapter.invoke({"op": "replay_trace", "trace": tampered_trace})
except RuntimeError:
    pass
else:
    raise AssertionError("Rust replay accepted forged recovery observations")

tampered_trace = copy.deepcopy(load_json(sorted(trace_root.glob("*.json"))[0]))
barrier = next(
    (event for event in tampered_trace["events"] if event["kind"] == "barrier_entered"),
    None,
)
if barrier is not None:
    barrier["data"]["result"] = "BOGUS"
    try:
        adapter.invoke({"op": "replay_trace", "trace": tampered_trace})
    except RuntimeError:
        pass
    else:
        raise AssertionError("Rust trace ingress accepted an invalid barrier result")

tampered_trace = copy.deepcopy(load_json(sorted(trace_root.glob("*.json"))[0]))
tampered_trace["budget"]["max_events"] = 129
try:
    adapter.invoke({"op": "replay_trace", "trace": tampered_trace})
except RuntimeError:
    pass
else:
    raise AssertionError("Rust replay accepted a budget above the model maximum")
print("PASS lifecycle-operation-boundary-contract backend=rust python=transport-only")
