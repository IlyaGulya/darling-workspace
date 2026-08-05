"""Behavioral contract for the deterministic dar-4ush.3 explorer."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.lifecycle_state_model import (  # noqa: E402
    TraceValidationError,
    invariant_names,
    load_json,
    replay_trace,
    validate_trace,
)

binary = Path(
    os.environ.get(
        "DARLING_LIFECYCLE_BOUNDARY_BIN",
        ROOT / "lifecycle" / "operation-boundary" / "target" / "debug" / "lifecycle-boundary",
    )
)
owned_tmp_root = Path(os.environ["DARLING_LIFECYCLE_CONTRACT_TMPDIR"]).resolve()
assert owned_tmp_root.is_dir()
assert Path(os.environ["TMPDIR"]).resolve() == owned_tmp_root
request = json.dumps({"op": "explore", "seed": 7}).encode()
explorer_env = os.environ.copy()
explorer_env["TMPDIR"] = str(owned_tmp_root)
first = subprocess.run(
    [str(binary)],
    input=request,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
    timeout=30,
    env=explorer_env,
)
assert first.returncode == 0, first.stderr.decode()
second = subprocess.run(
    [str(binary)],
    input=request,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
    timeout=30,
    env=explorer_env,
)
assert second.returncode == 0, second.stderr.decode()


def normalize_for_determinism(payload):
    """Ignore only disposable path and run-local identity evidence."""
    payload = json.loads(json.dumps(payload))
    for scenario in payload["scenarios"]:
        for field in ("expected_identity", "staged_identity", "obligation_identity"):
            if scenario[field]:
                scenario[field] = {"device": "<device>", "inode": "<inode>"}
        if scenario["forensic_root"]:
            scenario["forensic_root"] = "<forensic-root>"
        if scenario["diagnostic_error"]:
            scenario["diagnostic_error"] = re.sub(
                r'forensic_root=Some\("[^"]+"\)',
                'forensic_root=Some("<forensic-root>")',
                scenario["diagnostic_error"],
            )
    return payload


first_payload = json.loads(first.stdout)
second_payload = json.loads(second.stdout)
assert normalize_for_determinism(first_payload) == normalize_for_determinism(
    second_payload
), "same explorer seed changed semantic output"

report = first_payload
assert report["explorer_version"] == 2
assert report["seed"] == 7
assert report["scenario_count"] == 16 * 4 + 8 * 8
assert report["real_scenario_count"] == 8 * 4
assert report["reducer_only_alias_scenario_count"] == 8 * 4
assert report["reducer_only_scenario_count"] == 8 * 8
assert report["clean_count"] == 104
assert report["base_matrix_clean_count"] == 56
assert report["base_matrix_forensic_count"] == 8
assert report["reducer_only_matrix_clean_count"] == 48
assert report["reducer_only_matrix_forensic_count"] == 16
assert report["forensic_count"] == 24
assert report["unsafe_count"] == 0
assert report["undetected_count"] == 0
assert (
    report["clean_count"]
    + report["forensic_count"]
    + report["unsafe_count"]
    + report["undetected_count"]
    == report["scenario_count"]
)
assert len(report["declared_failure_boundaries"]) == 16
assert len(set(report["declared_failure_boundaries"])) == 16
assert report["real_failure_boundaries"] == report["declared_failure_boundaries"][:8]
assert report["reducer_only_failure_boundaries"] == report["declared_failure_boundaries"][8:]
assert report["representative_interleavings"] == [
    "none",
    "inode-aba",
    "partial-publication",
    "cleanup-failure",
]
assert len(set(report["representative_interleavings"])) == 4
assert set(report["reducer_only_interleavings"]) == {
    "orphan-cgroup",
    "post-deadline-signal",
    "restart",
    "late-fork",
    "root-exit",
    "pid-reuse",
    "fork-churn",
    "concurrent-close",
}
assert len(report["declared_operation_checkpoints"]) == 9
assert len(set(report["declared_operation_checkpoints"])) == 9

schema = load_json(ROOT / "schemas" / "lifecycle-replay-trace-v1.schema.json")
validator = Draft202012Validator(schema)
expected_invariants = set(invariant_names())
scenario_ids = set()
observed_checkpoints = set()
scenarios_by_id = {}


def assert_owned_forensic_root(value: str) -> Path:
    path = Path(value).resolve()
    try:
        path.relative_to(owned_tmp_root)
    except ValueError as error:
        raise AssertionError(
            f"forensic root escaped owned TMPDIR: {path} not under {owned_tmp_root}"
        ) from error
    assert path.is_dir()
    return path


for scenario in report["scenarios"]:
    reducer_fixture = scenario["interleaving"] in set(report["reducer_only_interleavings"])
    assert scenario["invariant_complete"] is True
    assert scenario["minimized_event_count"] <= scenario["event_count"]
    assert scenario["schedule_steps"] <= report["budget"]["max_schedule_steps"]
    trace = scenario["trace"]
    errors = list(validator.iter_errors(trace))
    assert not errors, (scenario["scenario"], errors)
    assert scenario["scenario"] == trace["scenario"]
    assert scenario["seed"] > 0
    assert scenario["virtual_time_ns"] <= report["budget"]["max_virtual_time_ns"]
    assert scenario["operation"] in {"MKDIR_CHILD", "UNLINK_EXACT", "RENAME_EXACT"}
    if scenario["failure_boundary"] in report["real_failure_boundaries"]:
        assert scenario["boundary_execution"] == "REAL_OPERATION_CHECKPOINT"
    else:
        assert scenario["boundary_execution"] == "REDUCER_ONLY_ALIAS"
    assert scenario["checkpoint"] in report["declared_operation_checkpoints"]
    assert scenario["placement"] in {"BEFORE", "AFTER"}
    assert scenario["probe_result"] == "INJECTED"
    assert scenario["real_checkpoint_observed"] is (not reducer_fixture)
    assert scenario["real_injection_observed"] is (not reducer_fixture)
    assert scenario["fault_detected"] is True
    assert scenario["safety_preserved"] is True
    assert scenario["recovery_status"] in {"CLEAN", "FORENSIC_REQUIRED"}
    assert scenario["operation_outcome"] in {
        "ERROR",
        "STAGED_ERROR",
        "ROLLED_BACK_ERROR",
        "ROLLBACK_INCOMPLETE",
        "IDENTITY_MISMATCH",
        "MUTATED_ERROR",
    }
    assert scenario["mutation_state"] in {
        "NOT_ATTEMPTED",
        "PARTIAL",
        "ROLLED_BACK",
        "ROLLBACK_INCOMPLETE",
    }
    assert scenario["interleaving_applied"] is (not reducer_fixture)
    assert isinstance(scenario["stage_obligations"], int)
    assert isinstance(scenario["quarantine_obligations"], int)
    assert len(scenario["stage_obligation_ids"]) == scenario["stage_obligations"]
    assert len(set(scenario["stage_obligation_ids"])) == len(
        scenario["stage_obligation_ids"]
    )
    assert len(scenario["quarantine_obligation_ids"]) == scenario[
        "quarantine_obligations"
    ]
    assert len(set(scenario["quarantine_obligation_ids"])) == len(
        scenario["quarantine_obligation_ids"]
    )
    assert scenario["filesystem_postcondition"] in {True, False}
    obligations = scenario["stage_obligations"] + scenario["quarantine_obligations"]
    expected_preserved = (
        obligations > 0
        or not scenario["filesystem_clean"]
        or not scenario["filesystem_postcondition"]
    )
    assert scenario["root_preserved"] is expected_preserved
    if obligations:
        assert scenario["root_preserved"] is True
    if scenario["recovery_status"] == "CLEAN":
        assert scenario["recovery_completed"] is True
        assert scenario["unresolved_obligations"] == []
        assert obligations == 0
        assert scenario["filesystem_clean"] is True
        assert scenario["filesystem_postcondition"] is True
        assert scenario["root_preserved"] is False
        assert scenario["forensic_root"] is None
        assert scenario["diagnostic_error"] is None
    else:
        if reducer_fixture:
            assert scenario["recovery_status"] == "FORENSIC_REQUIRED"
            assert scenario["unresolved_obligations"]
            assert scenario["recovery_completed"] is False
            assert obligations == 0
            assert scenario["filesystem_postcondition"] is True
            assert scenario["root_preserved"] is False
            assert scenario["forensic_root"] is None
            assert not (
                scenario["recovery_status"] == "CLEAN"
                and scenario["unresolved_obligations"]
            )
        else:
            # A retained stage is an intentional forensic outcome, not an
            # undetected fault.  The original inode and foreign replacement
            # must remain observable without any forward-finalization.
            assert scenario["recovery_status"] == "FORENSIC_REQUIRED"
            assert scenario["diagnostic_error"]
            assert scenario["forensic_root"]
            assert_owned_forensic_root(scenario["forensic_root"])
            assert scenario["root_preserved"] is True
            assert scenario["stage_obligations"] == 1
            assert scenario["quarantine_obligations"] == 0
            assert scenario["filesystem_postcondition"] is True
            assert scenario["expected_identity"] == scenario["staged_identity"]
            assert scenario["expected_identity"] == scenario["obligation_identity"]
            root = assert_owned_forensic_root(scenario["forensic_root"])
            stage_dirs = [
                path
                for path in root.iterdir()
                if path.name.startswith(".lifecycle-stage-")
            ]
            assert len(stage_dirs) == 1
            assert not any(
                path.name.startswith(".lifecycle-quarantine-") for path in root.iterdir()
            )
            entry = stage_dirs[0] / "entry"
            assert entry.is_file() and not entry.is_symlink()
            entry_stat = entry.stat()
            assert scenario["staged_identity"] == {
                "device": entry_stat.st_dev,
                "inode": entry_stat.st_ino,
            }
            if scenario["operation"] == "UNLINK_EXACT":
                assert entry.read_bytes() == b"target"
                assert (root / "target").read_bytes() in {
                    b"replacement",
                    b"cleanup-replacement",
                    b"publication-blocker",
                }
            elif scenario["operation"] == "RENAME_EXACT":
                assert entry.read_bytes() == b"source"
                assert (root / "source").read_bytes() in {
                    b"replacement",
                    b"cleanup-replacement",
                }
                assert not (root / "destination").exists()
            else:
                raise AssertionError("unexpected retained-stage operation")
    if reducer_fixture:
        assert scenario["boundary_execution"] == "REDUCER_ONLY_ALIAS"
        assert scenario["real_checkpoint_observed"] is False
        assert scenario["real_injection_observed"] is False
        assert scenario["interleaving_applied"] is False
        assert scenario["forensic_root"] is None
    assert scenario["typed_rejection_observed"] is True
    assert scenario["typed_recovery_observed"] is True
    checkpoint_events = [
        event for event in trace["events"] if event["kind"] == "operation_checkpoint"
    ]
    assert len(checkpoint_events) == 1
    checkpoint_data = checkpoint_events[0]["data"]
    assert checkpoint_data == {
        "operation": scenario["operation"],
        "checkpoint": scenario["checkpoint"],
        "placement": scenario["placement"],
        "result": scenario["probe_result"],
    }
    assert trace["events"][-1]["kind"] == "terminal"
    assert all(event["seq"] == index for index, event in enumerate(trace["events"]))
    assert all(
        left["time_ns"] <= right["time_ns"]
        for left, right in zip(trace["events"], trace["events"][1:])
    )
    replay = replay_trace(trace)
    assert replay.outcome == trace["expected"]["outcome"]
    assert replay.operation_rejection_seen is True
    assert replay.operation_recovery_seen is True
    assert "operation-failure" not in replay.obligations
    replay_unresolved = {
        item
        for item in replay.obligations
        if item.startswith("fault:")
        or item
        in {
            "identity-failure",
            "late-fork-recovery",
            "membership-incomplete",
            "endpoint-failure",
            "operation-failure",
        }
    }
    assert replay_unresolved == set(scenario["unresolved_obligations"])
    assert set(replay.satisfied_invariants) == expected_invariants
    assert replay.recovery_observations == tuple(
        (item["stable_state"], item["journal_phase"], item["action"])
        for item in trace["recovery_observations"]
    )
    scenario_ids.add(scenario["scenario"])
    scenarios_by_id[scenario["scenario"]] = scenario
    observed_checkpoints.add(scenario["checkpoint"])

assert observed_checkpoints == set(report["declared_operation_checkpoints"])
assert len(scenario_ids) == report["scenario_count"]
assert all(
    not (
        scenario["recovery_status"] == "CLEAN"
        and scenario["unresolved_obligations"]
    )
    for scenario in report["scenarios"]
), "CLEAN must never carry unresolved obligations"
for phase in ("prepare", "publish", "cleanup", "commit", "barrier", "identity", "signal", "endpoint"):
    before = scenarios_by_id[f"before-{phase}-none"]
    after = scenarios_by_id[f"after-{phase}-none"]
    assert before["placement"] == "BEFORE"
    assert after["placement"] == "AFTER"
    before_semantics = tuple(
        before[key]
        for key in (
            "operation",
            "checkpoint",
            "operation_outcome",
            "mutation_state",
            "stage_obligations",
            "quarantine_obligations",
            "filesystem_clean",
            "filesystem_postcondition",
            "root_preserved",
        )
    )
    after_semantics = tuple(
        after[key]
        for key in (
            "operation",
            "checkpoint",
            "operation_outcome",
            "mutation_state",
            "stage_obligations",
            "quarantine_obligations",
            "filesystem_clean",
            "filesystem_postcondition",
            "root_preserved",
        )
    )
    assert before_semantics != after_semantics, f"before/after {phase} collapsed"
    assert before["checkpoint"] and after["checkpoint"]

    def semantic_operation_events(scenario):
        events = []
        for event in scenario["trace"]["events"]:
            if event["kind"] not in {
                "operation_checkpoint",
                "fault_injected",
                "recovery",
                "endpoint_transition",
                "identity_revalidated",
            }:
                continue
            data = event["data"]
            state = event["state_after"]
            events.append(
                (
                    event["kind"],
                    data.get("operation"),
                    data.get("checkpoint"),
                    data.get("result"),
                    data.get("fault"),
                    data.get("action"),
                    state["stable_state"],
                    state["journal_phase"],
                )
            )
        return tuple(events)

    assert semantic_operation_events(before) != semantic_operation_events(after), (
        f"before/after {phase} have no normalized operation/checkpoint distinction"
    )

for boundary in report["declared_failure_boundaries"]:
    for interleaving in report["representative_interleavings"]:
        assert f"{boundary}-{interleaving}" in scenario_ids
for interleaving in report["reducer_only_interleavings"]:
    matches = [
        scenario for scenario in report["scenarios"] if scenario["interleaving"] == interleaving
    ]
    assert len(matches) == 8, (interleaving, len(matches))
    assert {
        scenario["failure_boundary"] for scenario in matches
    } == set(report["reducer_only_failure_boundaries"])
    assert all(
        scenario["boundary_execution"] == "REDUCER_ONLY_ALIAS" for scenario in matches
    )

cleanup_trace = next(
    scenario["trace"]
    for scenario in report["scenarios"]
    if scenario["scenario"] == "before-cleanup-cleanup-failure"
)
assert any(
    event["kind"] == "endpoint_transition"
    and event["data"]["result"] == "REJECTED"
    for event in cleanup_trace["events"]
), "cleanup fixture lacks the rejected endpoint witness"
assert cleanup_trace["recovery_observations"], "cleanup failure was not recovered"

tampered = json.loads(json.dumps(report["scenarios"][0]["trace"]))
tampered["expected"]["outcome"] = "SUCCESS"
try:
    validate_trace(tampered)
except TraceValidationError:
    pass
else:
    raise AssertionError("tampered explorer trace was accepted")

tampered_checkpoint = json.loads(json.dumps(report["scenarios"][0]["trace"]))
for event in tampered_checkpoint["events"]:
    if event["kind"] == "operation_checkpoint":
        event["data"]["result"] = "COMPLETED"
        break
else:
    raise AssertionError("explorer trace omitted operation checkpoint")
tampered_replay = replay_trace(tampered_checkpoint)
assert tampered_replay.operation_rejection_seen is False
assert tampered_replay.operation_recovery_seen is False

print(
    "PASS lifecycle-explorer-contract "
    f"scenarios={report['scenario_count']} clean={report['clean_count']} "
    f"forensic={report['forensic_count']} unsafe={report['unsafe_count']} "
    f"undetected={report['undetected_count']} "
    f"seed={report['seed']}"
)
