"""Authoritative lifecycle contract for producer-owned managed runs.

``managed_run`` owns the real run directory, the run lease, the lane, the guest
scope and every evidence/retention effect.  This module is the single
declarative semantic contract they share: the phases a persisted managed run may
occupy, the only admissible phase changes, the facts each persisted phase has to
prove, the tri-state command-launch convention and the takeover policy used by
recovery.  It is policy, not an engine: producers still perform every effect and
this module never touches the filesystem, processes or locks.

Two meanings are deliberately not collapsed:

* ``command_started`` is tri-state.  ``False`` means the guest command was not
  launched, ``True`` means it was, and ``None`` means the launch outcome is
  unknown and may have happened.  ``None`` is never interpreted as ``False``;
  an absent field keeps its historical reader meaning of ``False`` only because
  older unstarted records legitimately omitted it.
* ``completed`` means the *resource* lifecycle finished: the lane was retired,
  the payload was sealed and the external runtime references were handed over.
  It says nothing about workload PASS.  The original ``workload_rc``,
  cancellation signal, owner errors and cleanup problems remain the outcome.

Live ownership and readers always win over recovery.  A persisted record that
does not satisfy this contract is reported as unknown and preserved as bytes; it
is never adopted, normalised or rewritten as a success by either producer or
recovery.  The durable retirement rules (evidence journal, pins, reader leases,
image retention) stay in :mod:`evidence_retention` and
:mod:`runtime_retention`; this contract ends at the completed run record that
those modules hand off to.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1
KIND = "managed-lane-run"

PREPARING = "preparing"
CREATING = "creating"
RUNNING = "running"
CAPTURING = "capturing"
RETIRING = "retiring"
COMPLETED = "completed"
RETAINED = "retained"
BLOCKED = "blocked"

#: Synthetic report phase for an unreadable or foreign persisted record.  It is
#: never written: ``list_runs`` reports it and recovery skips it.
REPORT_UNKNOWN = "unknown"

PHASES = frozenset({
    PREPARING, CREATING, RUNNING, CAPTURING, RETIRING, COMPLETED, RETAINED, BLOCKED,
})
INITIAL_PHASE = PREPARING

NOT_STARTED = "not-started"
UNKNOWN = "unknown"
STARTED = "started"

#: Explicit phase changes the producer and recovery paths may persist.  A
#: same-phase save is not a transition; an omitted ``phase`` keeps the current
#: one.  ``COMPLETED`` has no outgoing edge: its outcome is immutable, and the
#: remaining work is external retention.
TRANSITIONS: dict[str | None, frozenset[str]] = {
    None: frozenset({PREPARING}),
    PREPARING: frozenset({CREATING, COMPLETED, BLOCKED}),
    CREATING: frozenset({CREATING, RUNNING, CAPTURING, RETAINED, COMPLETED, BLOCKED}),
    RUNNING: frozenset({CREATING, CAPTURING, RETAINED, COMPLETED, BLOCKED}),
    CAPTURING: frozenset({CAPTURING, CREATING, RETIRING, COMPLETED, BLOCKED}),
    RETIRING: frozenset({CREATING, CAPTURING, COMPLETED, BLOCKED}),
    RETAINED: frozenset({CREATING, RETIRING}),
    BLOCKED: frozenset({CREATING, CAPTURING, COMPLETED, BLOCKED}),
    COMPLETED: frozenset(),
}


SKIP = "skip"
KEEP = "keep"
RETIRE_KEPT = "retire-retained"
RECOVER = "recover"
PRESERVE_UNKNOWN = "preserve-unknown"

#: Once a run is ``completed`` its result bytes are the immutable outcome of the
#: resource lifecycle.  Only the external retention references (``pin``) may
#: still change.
OUTCOME_FIELDS = frozenset({
    "phase", "workload_rc", "cancel_signal", "error", "final_rc", "cleanup",
    "evidence", "completed", "host_streams_complete", "command_started",
})


class LifecycleError(ValueError):
    """A persisted record or requested phase change violates the contract."""


@dataclass(frozen=True)
class RecoveryPlan:
    action: str
    reason: str

# The phase policy declares decisions for (dead owner, live owner). Retained and
# blocked records transfer responsibility under the run lease; active phases do
# not. Explicit keep takes precedence, except that completed is always terminal.
_RESUME = RecoveryPlan(RECOVER, "owner responsibility ended; resume retirement")
_LIVE_OWNER = RecoveryPlan(KEEP, "owner identity is still live")
_EXPLICIT_KEEP = RecoveryPlan(KEEP, "explicitly retained")
_COMPLETED = RecoveryPlan(SKIP, "resource lifecycle is completed")
_RELEASE_KEPT = RecoveryPlan(RETIRE_KEPT, "release retained run through retirement")
_PHASE_RECOVERY = {
    PREPARING: (_RESUME, _LIVE_OWNER),
    CREATING: (_RESUME, _LIVE_OWNER),
    RUNNING: (_RESUME, _LIVE_OWNER),
    CAPTURING: (_RESUME, _LIVE_OWNER),
    RETIRING: (_RESUME, _LIVE_OWNER),
    RETAINED: (_RELEASE_KEPT, _RELEASE_KEPT),
    BLOCKED: (_RESUME, _RESUME),
    COMPLETED: (_COMPLETED, _COMPLETED),
}
# Mechanical expansion only: no effects or second recovery interpreter.
RECOVERY_TABLE = {
    (phase, keep_protected, owner_alive): (
        _EXPLICIT_KEEP if keep_protected and phase != COMPLETED else plans[owner_alive]
    )
    for phase, plans in _PHASE_RECOVERY.items()
    for keep_protected in (False, True)
    for owner_alive in (False, True)
}


def recorded_launch(record: Mapping[str, Any]) -> bool | None:
    """Return the persisted tri-state launch flag under historical semantics."""
    value = record.get("command_started", False)
    if value is False or value is True or value is None:
        return value
    raise LifecycleError(f"invalid command_started value: {value!r}")


def launch_state(record: Mapping[str, Any]) -> str:
    """Classify launch certainty; ``None`` is unknown, never not-started."""
    value = recorded_launch(record)
    if value is True:
        return STARTED
    if value is None:
        return UNKNOWN
    return NOT_STARTED


def phase_of(record: Mapping[str, Any]) -> str:
    return str(record.get("phase"))


def terminal(phase: str | None) -> bool:
    return phase == COMPLETED


def report_unknown(phase: str | None) -> bool:
    return phase == REPORT_UNKNOWN


def transition_allowed(before: str | None, after: str) -> bool:
    return after in TRANSITIONS.get(before, frozenset())


def validate_transition(before: str | None, after: str) -> None:
    if not transition_allowed(before, after):
        source = "absent" if before is None else before
        raise LifecycleError(f"managed run cannot change phase {source!r} to {after!r}")


def validate_update(before: str | None, changes: Mapping[str, Any]) -> None:
    """Reject writes that would rewrite a completed run's immutable outcome."""
    if before == COMPLETED and any(name in changes for name in OUTCOME_FIELDS):
        raise LifecycleError("completed run outcome is immutable")


def _integer(value: Any) -> bool:
    return type(value) is int


def _optional_int(record: Mapping[str, Any], name: str) -> bool:
    value = record.get(name)
    return value is None or _integer(value)


def _mapping(value: Any) -> bool:
    return isinstance(value, Mapping)


def _owned_path(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and Path(value).is_absolute()


def _owner_problems(owner: Any, label: str) -> list[str]:
    if owner is None:
        return []
    if not _mapping(owner):
        return [f"{label} is not a mapping"]
    problems = []
    pid = owner.get("pid")
    if not _integer(pid) or pid <= 0:
        problems.append(f"{label} has no positive pid")
    if not isinstance(owner.get("start_time"), str) or not owner["start_time"]:
        problems.append(f"{label} has no process start time")
    return problems


def record_problems(record: Mapping[str, Any]) -> list[str]:
    """Mechanical record validation shared by ``read_run`` and ``_save``.

    Absent optional fields keep their historically intentional reader meaning;
    present values must be well typed.  Only phase obligations that the real
    producers always publish are required, so legitimate older completed,
    retained and unstarted records still validate.
    """
    problems: list[str] = []
    if not _mapping(record):
        return ["managed run record is not a mapping"]
    if record.get("schema") != SCHEMA or not _integer(record.get("schema")):
        problems.append("managed run schema is not 1")
    if record.get("kind") != KIND:
        problems.append("managed run kind is not managed-lane-run")
    run_id = record.get("id")
    if not isinstance(run_id, str) or not run_id:
        problems.append("managed run has no id")
    if not _integer(record.get("uid")):
        problems.append("managed run has no numeric owner uid")
    root, path = record.get("root"), record.get("path")
    if not _owned_path(root):
        problems.append("managed run root is not an absolute path")
    if not _owned_path(path):
        problems.append("managed run path is not an absolute path")
    if _owned_path(root) and _owned_path(path) and isinstance(run_id, str) and run_id:
        run = Path(str(path))
        if run.parent != Path(str(root)) / "runs" or run.name != "run-" + run_id:
            problems.append("managed run path is not runs/run-<id> under its root")
    directory = record.get("directory")
    if (not _mapping(directory) or not _integer(directory.get("device"))
            or not _integer(directory.get("inode"))):
        problems.append("managed run directory identity is incomplete")

    lane = record.get("lane")
    if not _mapping(lane):
        problems.append("managed run has no owned lane namespace")
    else:
        lane_root, lane_path, lane_name = lane.get("root"), lane.get("path"), lane.get("name")
        if not _owned_path(lane_root) or not _owned_path(lane_path):
            problems.append("managed lane namespace is not an absolute path pair")
        elif not isinstance(lane_name, str) or not lane_name:
            problems.append("managed lane namespace has no name")
        else:
            if lane_path != str(Path(str(lane_root)) / lane_name):
                problems.append("managed lane path does not match its root and name")
            if _owned_path(root):
                owned = Path(str(root))
                selected = Path(str(lane_root))
                if selected != owned and owned not in selected.parents:
                    problems.append("managed lane root is outside the run owner root")
        if lane.get("token") != run_id:
            problems.append("managed lane token does not name this run")
        if ("device" in lane) != ("inode" in lane):
            problems.append("managed lane identity must carry both device and inode")
        elif "device" in lane and (not _integer(lane.get("device"))
                                   or not _integer(lane.get("inode"))):
            problems.append("managed lane identity is not numeric")

    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in PHASES:
        problems.append(f"managed run phase {phase!r} is not a persisted phase")
        return problems

    launch_invalid = False
    if "command_started" in record:
        try:
            recorded_launch(record)
        except LifecycleError as error:
            problems.append(str(error))
            launch_invalid = True
    if record.get("created") is not None and not isinstance(record.get("created"), str):
        problems.append("managed run creation time is not text")
    if record.get("updated") is not None and not isinstance(record.get("updated"), str):
        problems.append("managed run update time is not text")
    for name in ("keep", "pin", "host_streams_complete"):
        if name in record and not isinstance(record.get(name), bool):
            problems.append(f"managed run {name} is not boolean")
    for name in ("workload_rc", "final_rc"):
        if name in record and not _optional_int(record, name):
            problems.append(f"managed run {name} is not an integer or null")
    cancel = record.get("cancel_signal")
    if cancel is not None and (not _integer(cancel) or cancel <= 0):
        problems.append("managed run cancellation signal is not a positive integer")
    if record.get("error") is not None and not isinstance(record.get("error"), str):
        problems.append("managed run error is not text")
    if record.get("runtime_root") is not None and (
            not isinstance(record.get("runtime_root"), str) or not record["runtime_root"]):
        problems.append("managed run runtime root is not text or null")
    argv = record.get("argv")
    if argv is not None and (not isinstance(argv, list)
                             or not all(isinstance(item, str) for item in argv)):
        problems.append("managed run argv is not a string list")
    problems.extend(_owner_problems(record.get("owner"), "managed run owner"))
    problems.extend(_owner_problems(record.get("previous_owner"), "managed run previous owner"))

    evidence = record.get("evidence")
    if evidence is not None:
        if not _mapping(evidence):
            problems.append("managed run evidence record is not a mapping")
        elif "complete" in evidence and not isinstance(evidence.get("complete"), bool):
            problems.append("managed run evidence completeness is not boolean")
    cleanup = record.get("cleanup")
    if cleanup is not None:
        if not _mapping(cleanup):
            problems.append("managed run cleanup record is not a mapping")
        else:
            for name in ("success", "retained"):
                if name in cleanup and not isinstance(cleanup.get(name), bool):
                    problems.append(f"managed run cleanup {name} is not boolean")
            if "problems" in cleanup and not isinstance(cleanup.get("problems"), list):
                problems.append("managed run cleanup problems are not a list")

    launch = None if launch_invalid else launch_state(record)
    evidence_complete = _mapping(evidence) and evidence.get("complete") is True
    cleanup_success = _mapping(cleanup) and cleanup.get("success") is True
    cleanup_retained = _mapping(cleanup) and cleanup.get("retained") is True

    if phase in {PREPARING, CREATING}:
        if launch != NOT_STARTED:
            problems.append("an unstarted phase may only record a known not-started command")
        if record.get("workload_rc") is not None:
            problems.append("an unstarted phase cannot record a workload result")
    elif phase == CAPTURING:
        if launch == UNKNOWN:
            problems.append("capture requires a known command launch outcome")
    elif phase == COMPLETED:
        if not evidence_complete:
            problems.append("completed run has no complete evidence")
        if not cleanup_success:
            problems.append("completed run has not recorded successful cleanup")
        if record.get("keep") is True:
            problems.append("completed run is still marked keep")
        if record.get("host_streams_complete") is False:
            problems.append("completed run still reports incomplete host output")
        if "final_rc" in record and not _integer(record.get("final_rc")):
            problems.append("completed run final result is not an integer")
    elif phase == RETAINED:
        if record.get("keep") is not True:
            problems.append("retained run is not marked keep")
        if cleanup is not None and not cleanup_retained:
            problems.append("retained run has no retained cleanup record")
        if cleanup_success:
            problems.append("retained run did not stop its own scope")
    elif phase == BLOCKED:
        if cleanup_success:
            problems.append("blocked run cannot claim successful cleanup")
        if "final_rc" in record and not _integer(record.get("final_rc")):
            problems.append("blocked run final result is not an integer")
    return problems


def validate_record(record: Mapping[str, Any]) -> None:
    problems = record_problems(record)
    if problems:
        raise LifecycleError("; ".join(problems))


def require_capture_outcome(record: Mapping[str, Any]) -> None:
    """Refuse to seal lane evidence while the launch outcome is unknown."""
    if launch_state(record) == UNKNOWN:
        raise LifecycleError(
            "command launch outcome is unknown; refusing to seal evidence or retire the lane"
        )


def require_complete_evidence(evidence: Any) -> None:
    if not _mapping(evidence) or evidence.get("complete") is not True:
        raise LifecycleError("required run evidence is incomplete; lane/raw state retained")


def recovery_plan(
    record: Mapping[str, Any], *, release_kept: bool, owner_alive: bool,
) -> RecoveryPlan:
    """Decide what recovery may do with one leased persisted record."""
    phase = record.get("phase")
    if not isinstance(phase, str) or phase not in PHASES:
        return RecoveryPlan(PRESERVE_UNKNOWN, "record phase is not recognised; preserved")
    keep_protected = record.get("keep") is True and not release_kept
    return RECOVERY_TABLE[phase, keep_protected, owner_alive]
