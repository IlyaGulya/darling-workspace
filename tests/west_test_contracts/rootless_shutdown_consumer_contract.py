"""Focused first-consumer contract for dar-4ush.7 Rootless shutdown."""

from __future__ import annotations

import copy
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.lifecycle_operation_boundary import RustBoundaryAdapter  # noqa: E402
from west_commands.rootless_shutdown_lifecycle import (  # noqa: E402
    RootlessShutdownGoneConsumer,
    RootlessShutdownSignalConsumer,
    RootlessShutdownConsumerError,
)
from west_commands.lifecycle_state_model import load_json  # noqa: E402


binary = Path(
    os.environ.get(
        "DARLING_LIFECYCLE_BOUNDARY_BIN",
        ROOT / "lifecycle" / "operation-boundary" / "target" / "debug" / "lifecycle-boundary",
    )
)
consumer = RootlessShutdownSignalConsumer(
    ROOT,
    adapter=RustBoundaryAdapter(ROOT, binary=binary),
)
gone_consumer = RootlessShutdownGoneConsumer(
    ROOT,
    adapter=RustBoundaryAdapter(ROOT, binary=binary),
)

def rootless_fixture(filename: str, name: str) -> dict:
    trace = load_json(ROOT / "tests" / "fixtures" / "lifecycle-traces" / "v1" / filename)
    trace["trace_id"] = f"rootless-shutdown-{name}"
    trace["scenario"] = f"rootless-shutdown-{name}"
    trace["provenance"]["evidence_id"] = f"dar-4ush/rootless-shutdown/{name}"
    trace["provenance"]["source_identity"]["module"] = "rootless-shutdown"
    return trace


for filename in ("shared-session-signal-gone.json", "shared-session-signal-rejected.json"):
    trace = rootless_fixture(filename, filename.removesuffix(".json"))
    result = consumer.replay(trace)
    assert result["trace_id"] == trace["trace_id"]
    assert result["final_snapshot"]["journal_phase"] == "CLEANUP"
    assert result["satisfied_invariants"]

gone_trace = rootless_fixture(
    "session-root-exit-before-snapshot.json", "root-exit-before-snapshot"
)
gone_result = gone_consumer.replay(gone_trace)
assert gone_result["trace_id"] == gone_trace["trace_id"]
assert gone_result["outcome"] == "FAIL_CLOSED"
assert gone_result["final_snapshot"] == {
    "stable_state": "RUNNING",
    "journal_phase": "ABORT",
    "intent": "RECOVER",
}

missing_signal = rootless_fixture(
    "session-root-exit-before-snapshot.json", "root-exit-before-signal"
)
try:
    consumer.replay(missing_signal)
except RootlessShutdownConsumerError as error:
    assert "signal_sent" in str(error)
else:
    raise AssertionError("Rootless consumer accepted a trace without signal evidence")

try:
    gone_consumer.replay(rootless_fixture("shared-session-signal-gone.json", "signal-bearing"))
except RootlessShutdownConsumerError as error:
    assert "signal-bearing" in str(error)
else:
    raise AssertionError("Rootless GONE consumer accepted a signal-bearing trace")

bad_gone_result = copy.deepcopy(gone_trace)
bad_gone_result["events"][3]["data"]["result"] = "MATCH"
try:
    gone_consumer.replay(bad_gone_result)
except RootlessShutdownConsumerError as error:
    assert "session root" in str(error)
else:
    raise AssertionError("Rootless GONE consumer accepted a trace without GONE evidence")

member_gone = copy.deepcopy(gone_trace)
member_gone["initial"]["capability_catalog"][0]["kind"] = "SESSION_MEMBER_PIDFD"
try:
    gone_consumer.replay(member_gone)
except RootlessShutdownConsumerError as error:
    assert "SESSION_ROOT_PIDFD" in str(error)
else:
    raise AssertionError("Rootless GONE consumer accepted member-only disappearance")

duplicate_root = copy.deepcopy(gone_trace)
duplicate_root["initial"]["capability_catalog"].append(
    {
        "capability_id": "cap.second-root",
        "kind": "SESSION_ROOT_PIDFD",
        "identity": {"token": "second-root@startup", "generation": 1},
    }
)
try:
    gone_consumer.replay(duplicate_root)
except RootlessShutdownConsumerError as error:
    assert "one authoritative" in str(error)
else:
    raise AssertionError("Rootless GONE consumer accepted ambiguous root authority")

bad_shape = copy.deepcopy(
    rootless_fixture("shared-session-signal-gone.json", "bad-capability")
)
bad_shape["events"][4]["data"]["capability"] = "raw-pidfd"
try:
    consumer.replay(bad_shape)
except RootlessShutdownConsumerError as error:
    assert "capability" in str(error)
else:
    raise AssertionError("Rootless consumer accepted a non-capability signal target")

bad_type = copy.deepcopy(
    rootless_fixture("shared-session-signal-gone.json", "bad-capability-type")
)
bad_type["events"][4]["data"]["capability"] = 17
try:
    consumer.replay(bad_type)
except RootlessShutdownConsumerError as error:
    assert "capability" in str(error)
else:
    raise AssertionError("Rootless consumer accepted a non-string signal target")


def rejects_domain_mutation(trace: dict, expected: str, label: str) -> None:
    try:
        consumer.replay(trace)
    except RootlessShutdownConsumerError as error:
        assert expected in str(error), (label, error)
    else:
        raise AssertionError(f"Rootless consumer accepted {label}")


bad_create_prefix = copy.deepcopy(
    rootless_fixture("shared-session-signal-gone.json", "create-prefix")
)
bad_create_prefix["events"][0]["data"]["intent"] = "CREATE_PREFIX"
rejects_domain_mutation(bad_create_prefix, "REQUEST_SHUTDOWN", "CREATE_PREFIX intent")

bad_recreate_prefix = copy.deepcopy(
    rootless_fixture("shared-session-signal-gone.json", "recreate-prefix")
)
bad_recreate_prefix["events"][0]["data"]["intent"] = "RECREATE_PREFIX"
rejects_domain_mutation(bad_recreate_prefix, "REQUEST_SHUTDOWN", "RECREATE_PREFIX intent")

bad_profile = copy.deepcopy(
    rootless_fixture("shared-session-signal-gone.json", "wrong-profile")
)
bad_profile["provenance"]["source_identity"]["profile"] = "perf"
rejects_domain_mutation(bad_profile, "profile=rootless", "non-rootless profile")

print("PASS rootless-shutdown-consumer-contract backend=rust semantics=unchanged")
