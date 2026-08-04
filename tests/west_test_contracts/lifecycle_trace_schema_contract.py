"""Independent schema + executable-reducer contract for dar-4ush.1."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.lifecycle_state_model import (  # noqa: E402
    AnchoredCapability,
    CapabilityHandle,
    IntentKind,
    JournalPhase,
    RecoveryAction,
    StableState,
    StateSnapshot,
    TraceValidationError,
    apply_recovery_action,
    invariant_names,
    load_json,
    load_and_validate_golden_traces,
    recovery_matrix_pairs,
    replay_trace,
    validate_state_model,
    validate_trace,
)

state_schema = load_json(ROOT / "schemas" / "lifecycle-state-v1.schema.json")
trace_schema = load_json(ROOT / "schemas" / "lifecycle-replay-trace-v1.schema.json")
state_document = load_json(ROOT / "lifecycle" / "state-model-v1.json")


def schema_validator(schema: dict) -> Draft202012Validator:
    # Prove that the contract uses the complete Draft 2020-12 validator.
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


state_validator = schema_validator(state_schema)
trace_validator = schema_validator(trace_schema)
assert state_validator.is_valid(state_document)
validate_state_model(state_document)
assert state_schema["properties"]["schema_version"] == {"const": 1}
assert trace_schema["properties"]["model_version"] == {"const": 1}
assert trace_schema["properties"]["scenario"]["type"] == "string"
assert "enum" not in trace_schema["properties"]["scenario"]
assert trace_schema["properties"]["recovery_observations"].get("minItems", 0) == 0
assert "path" not in trace_schema["$defs"]["capability"]["properties"]
assert state_schema["properties"]["recovery_matrix"]["maxItems"] == 36

matrix = list(recovery_matrix_pairs())
assert len(matrix) == 36
assert len({(stable, phase) for stable, phase, _action in matrix}) == 36
for stable, phase, action in matrix:
    recovery = RecoveryAction(action)
    if recovery not in {RecoveryAction.NOOP, RecoveryAction.CONTINUE_DRAIN}:
        result = apply_recovery_action(StateSnapshot(StableState(stable), JournalPhase(phase), IntentKind.RECOVER), recovery, state_document)
        assert isinstance(result, StateSnapshot)

trace_paths = load_and_validate_golden_traces(ROOT)
assert {path.name for path in trace_paths} == {
    "session-root-exit-before-snapshot.json",
    "shared-session-retained-holder-timeout.json",
    "shared-session-pid-reuse.json",
    "shared-session-late-fork.json",
}

for trace_path in trace_paths:
    trace = load_json(trace_path)
    errors = list(trace_validator.iter_errors(trace))
    assert not errors, f"schema rejected golden trace {trace_path}: {errors}"
    replay = replay_trace(trace, state_document)
    assert replay == replay_trace(trace, state_document)
    assert replay.trace_id == trace["trace_id"]
    assert replay.outcome == trace["expected"]["outcome"]
    assert replay.final_snapshot.stable_state.value == trace["expected"]["stable_state"]
    assert replay.final_snapshot.journal_phase.value == trace["expected"]["journal_phase"]
    assert replay.final_snapshot.intent.value == trace["expected"]["intent"]
    assert replay.live_capabilities == tuple(sorted((item["capability"], item["owner"]) for item in trace["expected"]["live_capabilities"]))
    assert replay.recovery_observations == tuple(
        (item["stable_state"], item["journal_phase"], item["action"])
        for item in trace["recovery_observations"]
    )
    assert set(replay.satisfied_invariants) == set(invariant_names())
    assert all(event["seq"] == index for index, event in enumerate(trace["events"]))
    assert trace["events"][-1]["kind"] == "terminal"


def rejects_schema_and_python(mutated: dict, needle: str) -> None:
    assert list(trace_validator.iter_errors(mutated)), "schema accepted a differential negative"
    try:
        validate_trace(mutated, state_document)
    except TraceValidationError as error:
        assert needle in str(error), (needle, error)
    else:
        raise AssertionError("Python oracle accepted a schema differential negative")


root_exit = load_json(ROOT / "tests/fixtures/lifecycle-traces/v1/session-root-exit-before-snapshot.json")
bad_scenario = copy.deepcopy(root_exit)
bad_scenario["scenario"] = "Not A Scenario"
rejects_schema_and_python(bad_scenario, "scenario")

bad_provenance = copy.deepcopy(root_exit)
bad_provenance["provenance"]["kind"] = "invented"
rejects_schema_and_python(bad_provenance, "provenance.kind")

unbound_historical = copy.deepcopy(root_exit)
unbound_historical["provenance"]["kind"] = "historical-observation"
rejects_schema_and_python(unbound_historical, "provenance")

bad_barrier = copy.deepcopy(root_exit)
bad_barrier["events"][1]["data"]["result"] = "BOGUS"
rejects_schema_and_python(bad_barrier, "events.1.data.result")

bad_time_type = copy.deepcopy(root_exit)
bad_time_type["events"][0]["time_ns"] = True
rejects_schema_and_python(bad_time_type, "events.0.time_ns")

def rejects_python(mutated: dict, needle: str) -> None:
    try:
        validate_trace(mutated, state_document)
    except TraceValidationError as error:
        assert needle in str(error), (needle, error)
    else:
        raise AssertionError(f"negative accepted: {needle}")


early_terminal = copy.deepcopy(root_exit)
early_terminal["events"][1] = {
    "seq": 1,
    "time_ns": 1001,
    "actor": "recovery",
    "kind": "terminal",
    "data": {"outcome": "FAIL_CLOSED"},
    "state_after": copy.deepcopy(root_exit["events"][0]["state_after"]),
}
for index, event in enumerate(early_terminal["events"]):
    event["seq"] = index
rejects_python(early_terminal, "terminal event must be unique")


forged_live = copy.deepcopy(root_exit)
forged_live["expected"]["live_capabilities"] = [{"capability": "cap.session-root", "owner": "attacker"}]
rejects_python(forged_live, "expected live capabilities")

missing_invariants = copy.deepcopy(root_exit)
missing_invariants["expected"]["invariants"] = ["terminal-trace"]
rejects_python(missing_invariants, "complete executable invariant registry")

state_tamper = copy.deepcopy(root_exit)
state_tamper["events"][1]["state_after"]["stable_state"] = "RUNNING"
rejects_python(state_tamper, "state_after differs")

terminal_state_mutation = copy.deepcopy(root_exit)
terminal_state_mutation["events"][-1]["state_after"]["stable_state"] = "STOPPED"
rejects_python(terminal_state_mutation, "state_after differs")

double_acquire = copy.deepcopy(root_exit)
double_acquire["events"].insert(3, copy.deepcopy(double_acquire["events"][2]))
for index, event in enumerate(double_acquire["events"]):
    event["seq"] = index
rejects_python(double_acquire, "double-acquire")

use_after_release = copy.deepcopy(load_json(ROOT / "tests/fixtures/lifecycle-traces/v1/shared-session-retained-holder-timeout.json"))
release_index = next(i for i, event in enumerate(use_after_release["events"]) if event["kind"] == "capability_released")
signal = copy.deepcopy(use_after_release["events"][9])
signal["time_ns"] = use_after_release["events"][release_index]["time_ns"] + 1
use_after_release["events"].insert(release_index + 1, signal)
for index, event in enumerate(use_after_release["events"]):
    event["seq"] = index
rejects_python(use_after_release, "signal without live matching identity")

shared_signal = copy.deepcopy(load_json(ROOT / "tests/fixtures/lifecycle-traces/v1/shared-session-retained-holder-timeout.json"))
shared_signal["events"][9]["data"]["capability"] = "cap.shared-lease"
rejects_python(shared_signal, "process pidfd capability")

never_acquired_member = copy.deepcopy(load_json(ROOT / "tests/fixtures/lifecycle-traces/v1/shared-session-retained-holder-timeout.json"))
never_acquired_member["initial"]["capability_catalog"].append({"capability_id": "cap.never-acquired", "kind": "SESSION_MEMBER_PIDFD", "identity": {"token": "never@catalog", "generation": 1}})
never_acquired_member["events"][5]["data"]["members"].append("cap.never-acquired")
rejects_python(never_acquired_member, "not acquired")

reacquire = copy.deepcopy(load_json(ROOT / "tests/fixtures/lifecycle-traces/v1/shared-session-retained-holder-timeout.json"))
reacquire_event = copy.deepcopy(reacquire["events"][2])
reacquire_event["time_ns"] = 10001
reacquire["events"].insert(11, reacquire_event)
for index, event in enumerate(reacquire["events"]):
    event["seq"] = index
rejects_python(reacquire, "generation was already consumed")

wrong_observation = copy.deepcopy(root_exit)
wrong_observation["recovery_observations"][0]["stable_state"] = "UNINITIALIZED"
rejects_python(wrong_observation, "recovery observations differ")

too_large_budget = copy.deepcopy(root_exit)
too_large_budget["budget"]["max_events"] = 129
rejects_python(too_large_budget, "authoritative model maxima")

unresolved_success = copy.deepcopy(root_exit)
unresolved_success["events"].pop(4)  # no rollback after identity GONE
unresolved_success["events"][-1]["data"]["outcome"] = "SUCCESS"
unresolved_success["events"][-1]["state_after"] = copy.deepcopy(unresolved_success["events"][-2]["state_after"])
unresolved_success["expected"]["outcome"] = "SUCCESS"
unresolved_success["expected"]["stable_state"] = "DRAINING"
unresolved_success["expected"]["journal_phase"] = "PREPARE"
unresolved_success["expected"]["intent"] = "REQUEST_SHUTDOWN"
unresolved_success["recovery_observations"] = []
for index, event in enumerate(unresolved_success["events"]):
    event["seq"] = index
rejects_python(unresolved_success, "successful terminal has unresolved obligations")

no_recovery_success = copy.deepcopy(root_exit)
no_recovery_success["events"] = no_recovery_success["events"][:2]
no_recovery_success["events"].append({
    "seq": 2,
    "time_ns": 2000,
    "actor": "launcher",
    "kind": "terminal",
    "data": {"outcome": "SUCCESS"},
    "state_after": copy.deepcopy(no_recovery_success["events"][-1]["state_after"]),
})
no_recovery_success["expected"].update({"outcome": "SUCCESS", "stable_state": "DRAINING", "journal_phase": "PREPARE", "intent": "REQUEST_SHUTDOWN", "live_capabilities": []})
no_recovery_success["recovery_observations"] = []
for index, event in enumerate(no_recovery_success["events"]):
    event["seq"] = index
assert replay_trace(no_recovery_success, state_document).recovery_observations == ()

checkpoint_restore = copy.deepcopy(root_exit)
checkpoint_restore["initial"]["live_capabilities"] = [{"capability": "cap.session-root", "owner": "controller"}]
checkpoint_restore["events"].pop(2)  # ownership existed before this transaction
checkpoint_restore["expected"]["live_capabilities"] = [{"capability": "cap.session-root", "owner": "controller"}]
for index, event in enumerate(checkpoint_restore["events"]):
    event["seq"] = index
assert replay_trace(checkpoint_restore, state_document).live_capabilities == (("cap.session-root", "controller"),)

for trace_path in trace_paths:
    golden = load_json(trace_path)
    assert golden["provenance"]["kind"] == "golden-scenario"
    assert golden["provenance"]["source_label"] == "dar-4ush-golden-fixture"

capability = AnchoredCapability.from_mapping({
    "capability_id": "cap.contract",
    "kind": "SESSION_ROOT_PIDFD",
    "identity": {"token": "root@contract", "generation": 1},
})
handle = CapabilityHandle(capability)
assert handle.move() == capability
try:
    handle.move()
except RuntimeError as error:
    assert "moved twice" in str(error)
else:
    raise AssertionError("capability handle was copyable after move")

incomplete_model = copy.deepcopy(state_document)
incomplete_model["recovery_matrix"].pop()
try:
    validate_state_model(incomplete_model)
except TraceValidationError as error:
    assert "recovery matrix mismatch" in str(error)
else:
    raise AssertionError("incomplete recovery matrix was accepted")

bad_target_model = copy.deepcopy(state_document)
bad_target_model["recovery_targets"][4]["stable_state"] = "READY"
try:
    validate_state_model(bad_target_model)
except TraceValidationError as error:
    assert "recovery target semantics" in str(error)
else:
    raise AssertionError("inconsistent recovery target was accepted")

print(f"PASS lifecycle-trace-schema-contract traces={len(trace_paths)} recovery_pairs={len(matrix)} draft=2020-12 computed-replay=1")
