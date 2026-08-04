"""Independent, executable lifecycle state/replay oracle for dar-4ush.1.

The model is deliberately side-effect free.  A trace supplies observations,
but the reducer below owns the resulting state, journal, capability ownership,
obligations, and recovery decisions.  ``state_after`` and ``expected`` are
assertions about that computation, never inputs used to produce it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping


MODEL_VERSION = 1
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


class StableState(str, Enum):
    UNINITIALIZED = "UNINITIALIZED"
    READY = "READY"
    RUNNING = "RUNNING"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    CORRUPT = "CORRUPT"


class JournalPhase(str, Enum):
    NONE = "NONE"
    PREPARE = "PREPARE"
    PUBLISH = "PUBLISH"
    CLEANUP = "CLEANUP"
    COMMIT = "COMMIT"
    ABORT = "ABORT"


class IntentKind(str, Enum):
    NONE = "NONE"
    CREATE_PREFIX = "CREATE_PREFIX"
    START_SESSION = "START_SESSION"
    REQUEST_SHUTDOWN = "REQUEST_SHUTDOWN"
    RELEASE_SESSION = "RELEASE_SESSION"
    RECREATE_PREFIX = "RECREATE_PREFIX"
    RECOVER = "RECOVER"


class DecisionKind(str, Enum):
    ACQUIRE_CAPABILITY = "ACQUIRE_CAPABILITY"
    DECLARE_INTENT = "DECLARE_INTENT"
    ENTER_BARRIER = "ENTER_BARRIER"
    SNAPSHOT_MEMBERS = "SNAPSHOT_MEMBERS"
    REVALIDATE_IDENTITY = "REVALIDATE_IDENTITY"
    SIGNAL_MEMBER = "SIGNAL_MEMBER"
    REMOVE_ENDPOINT = "REMOVE_ENDPOINT"
    INJECT_FAULT = "INJECT_FAULT"
    RECOVER = "RECOVER"
    TERMINATE = "TERMINATE"


class CapabilityKind(str, Enum):
    PREFIX_DIRECTORY = "PREFIX_DIRECTORY"
    SESSION_ROOT_PIDFD = "SESSION_ROOT_PIDFD"
    SESSION_MEMBER_PIDFD = "SESSION_MEMBER_PIDFD"
    SHARED_SESSION_LEASE = "SHARED_SESSION_LEASE"
    RUNTIME_ENDPOINT = "RUNTIME_ENDPOINT"
    JOURNAL_RECORD = "JOURNAL_RECORD"


class RecoveryAction(str, Enum):
    INITIALIZE = "INITIALIZE"
    NOOP = "NOOP"
    ROLLBACK_UNINITIALIZED = "ROLLBACK_UNINITIALIZED"
    ROLLBACK_READY = "ROLLBACK_READY"
    ROLLBACK_RUNNING = "ROLLBACK_RUNNING"
    ROLLBACK_STOPPED = "ROLLBACK_STOPPED"
    COMPLETE_PUBLISH = "COMPLETE_PUBLISH"
    COMPLETE_CLEANUP = "COMPLETE_CLEANUP"
    DRAIN_TO_READY = "DRAIN_TO_READY"
    CONTINUE_DRAIN = "CONTINUE_DRAIN"
    FAIL_CLOSED = "FAIL_CLOSED"
    QUARANTINE = "QUARANTINE"


class TraceValidationError(ValueError):
    """A trace or model violates the independent oracle."""


@dataclass(frozen=True)
class StateSnapshot:
    stable_state: StableState
    journal_phase: JournalPhase
    intent: IntentKind

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StateSnapshot":
        _exact_keys(value, {"stable_state", "journal_phase", "intent"}, "snapshot")
        try:
            return cls(StableState(value["stable_state"]), JournalPhase(value["journal_phase"]), IntentKind(value["intent"]))
        except (KeyError, ValueError) as error:
            raise TraceValidationError(f"invalid snapshot: {value!r}") from error


@dataclass(frozen=True)
class CapabilityIdentity:
    token: str
    generation: int

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "CapabilityIdentity":
        _exact_keys(value, {"token", "generation"}, "capability identity")
        if not isinstance(value.get("token"), str) or not value["token"]:
            raise TraceValidationError("capability identity token must be non-empty")
        if isinstance(value.get("generation"), bool) or not isinstance(value.get("generation"), int) or value["generation"] < 1:
            raise TraceValidationError("capability identity generation must be a positive integer")
        return cls(value["token"], value["generation"])


@dataclass(frozen=True)
class AnchoredCapability:
    capability_id: str
    kind: CapabilityKind
    identity: CapabilityIdentity

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AnchoredCapability":
        _exact_keys(value, {"capability_id", "kind", "identity"}, "capability")
        capability_id = value.get("capability_id")
        if not isinstance(capability_id, str) or not capability_id.startswith("cap."):
            raise TraceValidationError(f"invalid capability id: {capability_id!r}")
        try:
            kind = CapabilityKind(value["kind"])
        except (KeyError, ValueError) as error:
            raise TraceValidationError(f"invalid capability kind: {value!r}") from error
        return cls(capability_id, kind, CapabilityIdentity.from_mapping(value["identity"]))


class CapabilityHandle:
    """A tiny one-way ownership handle used by direct unit contracts."""

    __slots__ = ("_capability", "_moved")

    def __init__(self, capability: AnchoredCapability):
        self._capability = capability
        self._moved = False

    def move(self) -> AnchoredCapability:
        if self._moved:
            raise RuntimeError("lifecycle capability was moved twice")
        self._moved = True
        return self._capability


@dataclass(frozen=True)
class CapabilityOwnership:
    capability: str
    owner: str


@dataclass(frozen=True)
class JournalIntent:
    kind: IntentKind
    transaction_id: str
    target_state: StableState


@dataclass(frozen=True)
class ReplayResult:
    trace_id: str
    outcome: str
    final_snapshot: StateSnapshot
    recovery_actions: tuple[str, ...]
    recovery_observations: tuple[tuple[str, str, str], ...]
    live_capabilities: tuple[tuple[str, str], ...]
    obligations: tuple[str, ...] = ()
    satisfied_invariants: tuple[str, ...] = ()


@dataclass
class _ReplayRuntime:
    snapshot: StateSnapshot
    catalog: dict[str, AnchoredCapability]
    live: dict[str, str]
    matched: set[str] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)
    obligations: set[str] = field(default_factory=set)
    recovery_actions: list[str] = field(default_factory=list)
    recovery_observations: list[tuple[str, str, str]] = field(default_factory=list)
    consumed_generations: dict[str, set[int]] = field(default_factory=dict)
    checkpoint_live: dict[str, str] = field(default_factory=dict)
    checkpoint_matched: set[str] = field(default_factory=set)
    checkpoint_gone: set[str] = field(default_factory=set)
    transaction_id: str | None = None
    membership_seen_after_gone: bool = False
    signal_without_identity: bool = False
    late_fork_seen: bool = False
    late_fork_fault_seen: bool = False
    membership_closed: bool = False
    late_fork_after_closed: bool = False
    terminal_outcome: str | None = None


def _exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    if set(value) != expected:
        raise TraceValidationError(f"{label} keys differ: expected {sorted(expected)}, got {sorted(value)}")


STABLE_STATES = tuple(state.value for state in StableState)
JOURNAL_PHASES = tuple(phase.value for phase in JournalPhase)
INTENT_KINDS = tuple(intent.value for intent in IntentKind)
DECISION_KINDS = tuple(decision.value for decision in DecisionKind)
CAPABILITY_KINDS = tuple(capability.value for capability in CapabilityKind)


def _build_recovery_matrix() -> dict[tuple[StableState, JournalPhase], RecoveryAction]:
    rows = {
        StableState.UNINITIALIZED: (RecoveryAction.INITIALIZE, RecoveryAction.ROLLBACK_UNINITIALIZED, RecoveryAction.ROLLBACK_UNINITIALIZED, RecoveryAction.ROLLBACK_UNINITIALIZED, RecoveryAction.FAIL_CLOSED, RecoveryAction.ROLLBACK_UNINITIALIZED),
        StableState.READY: (RecoveryAction.NOOP, RecoveryAction.ROLLBACK_READY, RecoveryAction.COMPLETE_PUBLISH, RecoveryAction.COMPLETE_CLEANUP, RecoveryAction.NOOP, RecoveryAction.ROLLBACK_READY),
        StableState.RUNNING: (RecoveryAction.NOOP, RecoveryAction.ROLLBACK_RUNNING, RecoveryAction.COMPLETE_PUBLISH, RecoveryAction.DRAIN_TO_READY, RecoveryAction.NOOP, RecoveryAction.ROLLBACK_RUNNING),
        StableState.DRAINING: (RecoveryAction.CONTINUE_DRAIN, RecoveryAction.ROLLBACK_RUNNING, RecoveryAction.COMPLETE_PUBLISH, RecoveryAction.CONTINUE_DRAIN, RecoveryAction.DRAIN_TO_READY, RecoveryAction.ROLLBACK_RUNNING),
        StableState.STOPPED: (RecoveryAction.NOOP, RecoveryAction.ROLLBACK_STOPPED, RecoveryAction.COMPLETE_PUBLISH, RecoveryAction.COMPLETE_CLEANUP, RecoveryAction.NOOP, RecoveryAction.ROLLBACK_STOPPED),
        StableState.CORRUPT: (RecoveryAction.FAIL_CLOSED, RecoveryAction.FAIL_CLOSED, RecoveryAction.FAIL_CLOSED, RecoveryAction.FAIL_CLOSED, RecoveryAction.FAIL_CLOSED, RecoveryAction.QUARANTINE),
    }
    matrix = {(state, phase): actions[index] for state, actions in rows.items() for index, phase in enumerate(JournalPhase)}
    if set(matrix) != {(state, phase) for state in StableState for phase in JournalPhase} or len(matrix) != 36:
        raise AssertionError("lifecycle recovery matrix is not total")
    return matrix


RECOVERY_MATRIX = _build_recovery_matrix()


def recovery_action(stable_state: str | StableState, journal_phase: str | JournalPhase) -> RecoveryAction:
    return RECOVERY_MATRIX[(StableState(stable_state), JournalPhase(journal_phase))]


# Recovery is allowed to move from a drain to the running rollback state and
# from a running cleanup to ready.  These were previously omitted, making two
# matrix actions unexecutable.
_STABLE_TRANSITIONS = {
    StableState.UNINITIALIZED: {StableState.UNINITIALIZED, StableState.READY, StableState.CORRUPT},
    StableState.READY: {StableState.READY, StableState.RUNNING, StableState.STOPPED, StableState.CORRUPT},
    StableState.RUNNING: {StableState.RUNNING, StableState.DRAINING, StableState.READY, StableState.STOPPED, StableState.CORRUPT},
    StableState.DRAINING: {StableState.DRAINING, StableState.RUNNING, StableState.READY, StableState.STOPPED, StableState.CORRUPT},
    StableState.STOPPED: {StableState.STOPPED, StableState.READY, StableState.CORRUPT},
    StableState.CORRUPT: {StableState.CORRUPT},
}
_JOURNAL_TRANSITIONS = {
    JournalPhase.NONE: {JournalPhase.NONE, JournalPhase.PREPARE, JournalPhase.COMMIT, JournalPhase.ABORT},
    JournalPhase.PREPARE: {JournalPhase.PREPARE, JournalPhase.PUBLISH, JournalPhase.CLEANUP, JournalPhase.ABORT},
    JournalPhase.PUBLISH: {JournalPhase.PUBLISH, JournalPhase.COMMIT, JournalPhase.ABORT},
    JournalPhase.CLEANUP: {JournalPhase.CLEANUP, JournalPhase.COMMIT, JournalPhase.ABORT},
    JournalPhase.COMMIT: {JournalPhase.COMMIT, JournalPhase.NONE, JournalPhase.ABORT},
    JournalPhase.ABORT: {JournalPhase.ABORT, JournalPhase.NONE},
}

_EVENT_KINDS = {
    "capability_acquired", "capability_moved", "capability_released", "intent_declared", "barrier_entered",
    "membership_snapshot", "identity_revalidated", "signal_sent", "endpoint_transition", "member_observed",
    "fault_injected", "recovery", "terminal",
}
_ACTORS = {"launcher", "controller", "launchd", "member", "observer", "recovery"}
_FORBIDDEN_AUTHORITY_KEYS = {"path", "raw_path", "fd", "directory_fd", "unvalidated_fd"}
_INVARIANTS = {
    "no-raw-path-authority", "no-unvalidated-fd", "stable-journal-independent", "total-recovery-matrix",
    "identity-before-signal", "no-children-after-gone", "shared-lease-bound", "pidfd-identity-before-signal",
    "late-fork-closes-snapshot", "bounded-replay", "terminal-trace",
}
_OWNER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SCENARIO_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PIDFD_KINDS = {CapabilityKind.SESSION_ROOT_PIDFD, CapabilityKind.SESSION_MEMBER_PIDFD}
_IDENTITY_KINDS = _PIDFD_KINDS | {CapabilityKind.SHARED_SESSION_LEASE}
MODEL_BUDGETS = {
    "max_events": 128,
    "max_virtual_time_ns": 1_000_000,
    "max_live_capabilities": 64,
    "max_recovery_steps": 16,
}
_UNRESOLVED_OBLIGATIONS = {"identity-failure", "late-fork-recovery", "membership-incomplete"}


def _recovery_target(document: Mapping[str, Any], action: RecoveryAction) -> StateSnapshot:
    for target in document.get("recovery_targets", []):
        if target.get("action") == action.value:
            return StateSnapshot.from_mapping({"stable_state": target["stable_state"], "journal_phase": target["journal_phase"], "intent": target["intent"]})
    raise TraceValidationError(f"recovery target missing for {action.value}")


def apply_recovery_action(snapshot: StateSnapshot, action: RecoveryAction, model: Mapping[str, Any] | None = None) -> StateSnapshot:
    """Compute a recovery result from the pre-action state; never trust a trace target."""
    if action is RecoveryAction.NOOP or action is RecoveryAction.CONTINUE_DRAIN:
        return snapshot
    targets = {
        RecoveryAction.INITIALIZE: StateSnapshot(StableState.READY, JournalPhase.COMMIT, IntentKind.CREATE_PREFIX),
        RecoveryAction.ROLLBACK_UNINITIALIZED: StateSnapshot(StableState.UNINITIALIZED, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.ROLLBACK_READY: StateSnapshot(StableState.READY, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.ROLLBACK_RUNNING: StateSnapshot(StableState.RUNNING, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.ROLLBACK_STOPPED: StateSnapshot(StableState.STOPPED, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.COMPLETE_PUBLISH: StateSnapshot(StableState.READY, JournalPhase.COMMIT, IntentKind.CREATE_PREFIX),
        RecoveryAction.COMPLETE_CLEANUP: StateSnapshot(StableState.STOPPED, JournalPhase.COMMIT, IntentKind.RELEASE_SESSION),
        RecoveryAction.DRAIN_TO_READY: StateSnapshot(StableState.READY, JournalPhase.COMMIT, IntentKind.RELEASE_SESSION),
        RecoveryAction.FAIL_CLOSED: StateSnapshot(StableState.CORRUPT, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.QUARANTINE: StateSnapshot(StableState.CORRUPT, JournalPhase.ABORT, IntentKind.RECOVER),
    }
    if action not in targets:
        raise TraceValidationError(f"no executable target for recovery action {action.value}")
    result = targets[action]
    if result.stable_state not in _STABLE_TRANSITIONS[snapshot.stable_state]:
        raise TraceValidationError(f"recovery action {action.value} is not reachable from {snapshot}")
    if result.journal_phase not in _JOURNAL_TRANSITIONS[snapshot.journal_phase]:
        raise TraceValidationError(f"recovery journal target {action.value} is not reachable from {snapshot}")
    return result


def _canonical_recovery_target(action: RecoveryAction) -> StateSnapshot | None:
    """Return the action's fixed target; dynamic actions return ``None``."""
    if action in {RecoveryAction.NOOP, RecoveryAction.CONTINUE_DRAIN}:
        return None
    return {
        RecoveryAction.INITIALIZE: StateSnapshot(StableState.READY, JournalPhase.COMMIT, IntentKind.CREATE_PREFIX),
        RecoveryAction.ROLLBACK_UNINITIALIZED: StateSnapshot(StableState.UNINITIALIZED, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.ROLLBACK_READY: StateSnapshot(StableState.READY, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.ROLLBACK_RUNNING: StateSnapshot(StableState.RUNNING, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.ROLLBACK_STOPPED: StateSnapshot(StableState.STOPPED, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.COMPLETE_PUBLISH: StateSnapshot(StableState.READY, JournalPhase.COMMIT, IntentKind.CREATE_PREFIX),
        RecoveryAction.COMPLETE_CLEANUP: StateSnapshot(StableState.STOPPED, JournalPhase.COMMIT, IntentKind.RELEASE_SESSION),
        RecoveryAction.DRAIN_TO_READY: StateSnapshot(StableState.READY, JournalPhase.COMMIT, IntentKind.RELEASE_SESSION),
        RecoveryAction.FAIL_CLOSED: StateSnapshot(StableState.CORRUPT, JournalPhase.ABORT, IntentKind.RECOVER),
        RecoveryAction.QUARANTINE: StateSnapshot(StableState.CORRUPT, JournalPhase.ABORT, IntentKind.RECOVER),
    }[action]


def validate_state_model(document: Mapping[str, Any]) -> None:
    required = {"schema_version", "kind", "stable_states", "journal_phases", "intent_kinds", "decision_kinds", "capability_kinds", "budgets", "recovery_targets", "recovery_matrix", "invariants"}
    _exact_keys(document, required, "lifecycle state model")
    if document["schema_version"] != MODEL_VERSION or document["kind"] != "lifecycle-state-model":
        raise TraceValidationError("unsupported lifecycle state model version")
    registries = (("stable_states", STABLE_STATES), ("journal_phases", JOURNAL_PHASES), ("intent_kinds", INTENT_KINDS), ("decision_kinds", DECISION_KINDS), ("capability_kinds", CAPABILITY_KINDS))
    for name, expected in registries:
        if tuple(document[name]) != expected:
            raise TraceValidationError(f"{name} registry differs from the Python model")
    if set(document["invariants"]) != _INVARIANTS:
        raise TraceValidationError("invariant registry differs from the Python model")
    if document["budgets"] != MODEL_BUDGETS:
        raise TraceValidationError("model budgets differ from the authoritative Python maxima")
    targets = {RecoveryAction(entry["action"]): StateSnapshot.from_mapping({"stable_state": entry["stable_state"], "journal_phase": entry["journal_phase"], "intent": entry["intent"]}) for entry in document["recovery_targets"]}
    if set(targets) != set(RecoveryAction):
        raise TraceValidationError("recovery target registry is incomplete")
    for action, target in targets.items():
        canonical = _canonical_recovery_target(action)
        if canonical is not None and target != canonical:
            raise TraceValidationError(f"recovery target semantics differ for {action.value}")
    observed: dict[tuple[StableState, JournalPhase], RecoveryAction] = {}
    for entry in document["recovery_matrix"]:
        _exact_keys(entry, {"stable_state", "journal_phase", "action"}, "recovery entry")
        key = (StableState(entry["stable_state"]), JournalPhase(entry["journal_phase"]))
        if key in observed:
            raise TraceValidationError(f"duplicate recovery entry: {key}")
        observed[key] = RecoveryAction(entry["action"])
    if observed != RECOVERY_MATRIX:
        raise TraceValidationError("recovery matrix mismatch")
    for (stable, phase), action in RECOVERY_MATRIX.items():
        # NOOP/CONTINUE are deliberately dynamic; every other action must
        # have a legal executable target for every pair where it is selected.
        if action not in {RecoveryAction.NOOP, RecoveryAction.CONTINUE_DRAIN}:
            apply_recovery_action(StateSnapshot(stable, phase, IntentKind.RECOVER), action, document)


def _reject_authority_fields(value: Any, location: str) -> None:
    if isinstance(value, Mapping):
        forbidden = _FORBIDDEN_AUTHORITY_KEYS & set(value)
        if forbidden:
            raise TraceValidationError(f"{location} contains raw authority fields: {sorted(forbidden)}")
        for key, child in value.items():
            _reject_authority_fields(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_authority_fields(child, f"{location}[{index}]")


def _ownership(value: Mapping[str, Any], label: str = "ownership") -> CapabilityOwnership:
    _exact_keys(value, {"capability", "owner"}, label)
    capability, owner = value.get("capability"), value.get("owner")
    if not isinstance(capability, str) or not capability.startswith("cap.") or not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
        raise TraceValidationError(f"invalid {label}")
    return CapabilityOwnership(capability, owner)


def _validate_event_data(kind: str, data: Mapping[str, Any], catalog: Mapping[str, AnchoredCapability]) -> None:
    required = {
        "capability_acquired": {"capability", "owner"}, "capability_moved": {"capability", "from", "to"}, "capability_released": {"capability", "owner", "reason"},
        "intent_declared": {"intent", "transaction_id"}, "barrier_entered": {"barrier", "result"}, "membership_snapshot": {"authority", "members", "completeness"},
        "identity_revalidated": {"capability", "result"}, "signal_sent": {"capability", "signal", "result"}, "endpoint_transition": {"endpoint", "operation", "result"},
        "member_observed": {"capability", "origin"}, "fault_injected": {"phase", "fault"}, "recovery": {"action", "reason"}, "terminal": {"outcome"},
    }
    _exact_keys(data, required[kind], f"{kind} data")
    if kind in {"capability_acquired", "capability_moved", "capability_released"}:
        cap = data["capability"]
        if cap not in catalog:
            raise TraceValidationError(f"{kind} references unknown catalog capability {cap}")
        owners = [data[k] for k in ("owner",) if k in data] + [data[k] for k in ("from", "to") if k in data]
        if any(not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner) for owner in owners):
            raise TraceValidationError("invalid capability owner")
        if kind == "capability_released" and (not isinstance(data["reason"], str) or not re.fullmatch(r"^[a-z0-9._-]+$", data["reason"])):
            raise TraceValidationError("invalid capability release reason")
    elif kind == "intent_declared":
        if data["intent"] == "NONE" or not isinstance(data["transaction_id"], str) or not data["transaction_id"].startswith("txn."):
            raise TraceValidationError("invalid declared intent")
    elif kind == "barrier_entered":
        if data["barrier"] not in {"THREADS_STOPPED", "SESSION_QUIESCED", "MEMBERSHIP_SNAPSHOT"} or data["result"] not in {"ENTERED", "GONE", "TIMEOUT", "REJECTED"}:
            raise TraceValidationError("unknown lifecycle barrier/result")
    elif kind == "membership_snapshot":
        if data["authority"] not in {"SESSION_LEDGER", "DELEGATED_CGROUP", "PROC_TASK_CHILDREN"} or data["completeness"] not in {"COMPLETE", "CLOSED", "ABORTED_GONE", "OVERFLOW", "TIMEOUT"}:
            raise TraceValidationError("unknown membership snapshot")
        if any(member not in catalog for member in data["members"]):
            raise TraceValidationError("membership snapshot references unknown capability")
    elif kind == "identity_revalidated":
        if data["capability"] not in catalog or data["result"] not in {"MATCH", "MISMATCH", "GONE"}:
            raise TraceValidationError("invalid identity revalidation")
    elif kind == "signal_sent":
        if data["capability"] not in catalog or data["signal"] not in {"TERM", "KILL", "STOP", "CONT"} or data["result"] not in {"SENT", "GONE", "REJECTED", "DEADLINE"}:
            raise TraceValidationError("invalid signal event")
    elif kind == "endpoint_transition":
        if not isinstance(data["endpoint"], str) or not data["endpoint"].startswith("endpoint.") or data["operation"] not in {"CLOSE", "UNLINK", "REVALIDATE"} or data["result"] not in {"REMOVED", "ALREADY_GONE", "MISMATCH", "REJECTED"}:
            raise TraceValidationError("invalid endpoint transition")
    elif kind == "member_observed":
        if data["capability"] not in catalog or data["origin"] not in {"STARTUP_LEDGER", "SNAPSHOT", "LATE_FORK", "REPLACEMENT"}:
            raise TraceValidationError("invalid member observation")
    elif kind == "fault_injected":
        if data["phase"] not in {"PREPARE", "PUBLISH", "CLEANUP", "COMMIT", "BARRIER", "SIGNAL"} or data["fault"] not in {"SIGINT", "TIMEOUT", "ROOT_EXIT", "PID_REUSE", "LATE_FORK", "LEASE_HOLDER", "CLOSE_RANGE"}:
            raise TraceValidationError("invalid fault")
    elif kind == "recovery":
        RecoveryAction(data["action"])
    elif kind == "terminal" and data["outcome"] not in {"SUCCESS", "FAIL_CLOSED", "TIMEOUT", "RECOVERED"}:
        raise TraceValidationError("unknown terminal outcome")


def apply_event(runtime: _ReplayRuntime, event: Mapping[str, Any], model: Mapping[str, Any] | None = None) -> None:
    kind, data = event["kind"], event["data"]
    before = runtime.snapshot
    if kind == "capability_acquired":
        cap, owner = data["capability"], data["owner"]
        if cap in runtime.live:
            raise TraceValidationError(f"double-acquire of live capability {cap}")
        generation = runtime.catalog[cap].identity.generation
        if generation in runtime.consumed_generations.get(cap, set()):
            raise TraceValidationError(f"capability generation was already consumed: {cap}@{generation}")
        runtime.consumed_generations.setdefault(cap, set()).add(generation)
        runtime.live[cap] = owner
    elif kind == "capability_moved":
        cap = data["capability"]
        if cap not in runtime.live:
            raise TraceValidationError(f"move of non-live capability {cap}")
        if runtime.live[cap] != data["from"]:
            raise TraceValidationError(f"capability move owner mismatch for {cap}")
        runtime.live[cap] = data["to"]
    elif kind == "capability_released":
        cap = data["capability"]
        if cap not in runtime.live:
            raise TraceValidationError(f"release of non-live capability {cap}")
        if runtime.live[cap] != data["owner"]:
            raise TraceValidationError(f"capability release owner mismatch for {cap}")
        del runtime.live[cap]
        runtime.matched.discard(cap)
    elif kind == "intent_declared":
        if runtime.transaction_id is not None:
            raise TraceValidationError("nested lifecycle transaction")
        runtime.transaction_id = data["transaction_id"]
        runtime.checkpoint_live = dict(runtime.live)
        runtime.checkpoint_matched = set(runtime.matched)
        runtime.checkpoint_gone = set(runtime.gone)
        runtime.snapshot = StateSnapshot(runtime.snapshot.stable_state, JournalPhase.PREPARE, IntentKind(data["intent"]))
    elif kind == "barrier_entered":
        if data["result"] == "ENTERED":
            runtime.snapshot = StateSnapshot(StableState.DRAINING, runtime.snapshot.journal_phase, runtime.snapshot.intent)
    elif kind == "membership_snapshot":
        if runtime.gone:
            runtime.membership_seen_after_gone = True
        if any(member not in runtime.live for member in data["members"]):
            raise TraceValidationError("membership snapshot references a capability that was not acquired")
        if any(runtime.catalog[member].kind not in _PIDFD_KINDS for member in data["members"]):
            raise TraceValidationError("membership snapshot contains a non-process capability")
        if data["completeness"] == "CLOSED":
            runtime.membership_closed = True
        if data["completeness"] in {"TIMEOUT", "OVERFLOW"}:
            runtime.obligations.add("membership-incomplete")
        else:
            runtime.obligations.add("membership-closed")
    elif kind == "identity_revalidated":
        cap, result = data["capability"], data["result"]
        if cap not in runtime.live:
            raise TraceValidationError(f"identity revalidation after release: {cap}")
        if runtime.catalog[cap].kind not in _IDENTITY_KINDS:
            raise TraceValidationError(f"identity revalidation is not defined for capability kind {runtime.catalog[cap].kind.value}")
        if result == "MATCH":
            runtime.matched.add(cap)
        else:
            runtime.gone.add(cap)
            runtime.live.pop(cap, None)
            runtime.matched.discard(cap)
            runtime.obligations.add("identity-failure")
    elif kind == "signal_sent":
        cap = data["capability"]
        if cap not in runtime.live or cap not in runtime.matched:
            runtime.signal_without_identity = True
            raise TraceValidationError(f"signal without live matching identity: {cap}")
        if runtime.catalog[cap].kind not in _PIDFD_KINDS:
            raise TraceValidationError(f"signal requires a process pidfd capability, got {runtime.catalog[cap].kind.value}")
        runtime.snapshot = StateSnapshot(runtime.snapshot.stable_state, JournalPhase.CLEANUP, runtime.snapshot.intent)
    elif kind == "member_observed" and data["origin"] == "LATE_FORK":
        if runtime.catalog[data["capability"]].kind not in _PIDFD_KINDS:
            raise TraceValidationError("member observation requires a process pidfd capability")
        runtime.late_fork_seen = True
        runtime.late_fork_after_closed = runtime.membership_closed
        runtime.obligations.add("late-fork-recovery")
    elif kind == "fault_injected":
        runtime.obligations.add(f"fault:{data['fault']}")
        if data["fault"] == "LATE_FORK":
            runtime.late_fork_fault_seen = True
    elif kind == "recovery":
        action = RecoveryAction(data["action"])
        expected = recovery_action(runtime.snapshot.stable_state, runtime.snapshot.journal_phase)
        if action is not expected:
            raise TraceValidationError(f"recovery action {action.value} does not cover {runtime.snapshot.stable_state.value}/{runtime.snapshot.journal_phase.value}; expected {expected.value}")
        runtime.recovery_observations.append((runtime.snapshot.stable_state.value, runtime.snapshot.journal_phase.value, action.value))
        runtime.snapshot = apply_recovery_action(runtime.snapshot, action, model)
        runtime.recovery_actions.append(action.value)
        if action in {RecoveryAction.ROLLBACK_UNINITIALIZED, RecoveryAction.ROLLBACK_READY, RecoveryAction.ROLLBACK_RUNNING, RecoveryAction.ROLLBACK_STOPPED, RecoveryAction.FAIL_CLOSED, RecoveryAction.QUARANTINE}:
            # Rollback owns the release boundary, but restores the exact
            # ownership checkpoint rather than erasing unrelated pre-existing
            # capabilities.  Generation consumption remains monotonic.
            runtime.live = dict(runtime.checkpoint_live)
            runtime.matched = set(runtime.checkpoint_matched)
            runtime.gone = set(runtime.checkpoint_gone)
            runtime.transaction_id = None
        runtime.obligations.difference_update({item for item in runtime.obligations if item.startswith("fault:") or item in {"late-fork-recovery", "identity-failure"}})
    elif kind == "terminal":
        if runtime.terminal_outcome is not None:
            raise TraceValidationError("trace contains more than one terminal event")
        runtime.terminal_outcome = data["outcome"]

    if runtime.snapshot.stable_state not in _STABLE_TRANSITIONS[before.stable_state]:
        raise TraceValidationError(f"illegal stable transition {before.stable_state.value} -> {runtime.snapshot.stable_state.value}")
    if runtime.snapshot.journal_phase not in _JOURNAL_TRANSITIONS[before.journal_phase]:
        raise TraceValidationError(f"illegal journal transition {before.journal_phase.value} -> {runtime.snapshot.journal_phase.value}")

    recorded = StateSnapshot.from_mapping(event["state_after"])
    if recorded != runtime.snapshot:
        raise TraceValidationError(f"event {event['seq']} state_after differs from computed reducer state: recorded={recorded}, computed={runtime.snapshot}")


def _invariants(runtime: _ReplayRuntime, document: Mapping[str, Any], budget: Mapping[str, Any]) -> set[str]:
    events = document["events"]
    satisfied = {
        "no-raw-path-authority": True,
        "no-unvalidated-fd": not runtime.signal_without_identity,
        "stable-journal-independent": all(set(event["state_after"]) == {"stable_state", "journal_phase", "intent"} for event in events),
        "total-recovery-matrix": len(RECOVERY_MATRIX) == 36,
        "identity-before-signal": not runtime.signal_without_identity,
        "no-children-after-gone": not runtime.membership_seen_after_gone,
        "shared-lease-bound": not (runtime.terminal_outcome == "SUCCESS" and any(runtime.catalog[cap].kind is CapabilityKind.SHARED_SESSION_LEASE for cap in runtime.live)),
        "pidfd-identity-before-signal": not runtime.signal_without_identity,
        "late-fork-closes-snapshot": not runtime.late_fork_seen or (runtime.late_fork_after_closed and runtime.late_fork_fault_seen and "late-fork-recovery" not in runtime.obligations and bool(runtime.recovery_actions)),
        "bounded-replay": len(events) <= budget["max_events"] and events[-1]["time_ns"] <= budget["max_virtual_time_ns"] and len(runtime.live) <= budget["max_live_capabilities"] and len(runtime.recovery_actions) <= budget["max_recovery_steps"],
        "terminal-trace": bool(events) and events[-1]["kind"] == "terminal" and runtime.terminal_outcome is not None,
    }
    return {name for name, ok in satisfied.items() if ok}


def validate_trace(
    document: Mapping[str, Any],
    model: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> None:
    replay_trace(document, model, schema)


def validate_trace_schema(document: Mapping[str, Any], schema: Mapping[str, Any] | None = None) -> None:
    """Run the same Draft 2020-12 gate used by the contract before replay."""
    try:
        from jsonschema import Draft202012Validator
    except ImportError as error:  # pragma: no cover - the wrapper supplies the pinned dependency
        raise TraceValidationError("Draft 2020-12 validator dependency is unavailable") from error
    if schema is None:
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "lifecycle-replay-trace-v1.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    errors = sorted(Draft202012Validator(schema).iter_errors(document), key=lambda item: list(item.absolute_path))
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path) or "trace"
        raise TraceValidationError(f"schema validation failed at {location}: {error.message}")


def replay_trace(
    document: Mapping[str, Any],
    model: Mapping[str, Any] | None = None,
    schema: Mapping[str, Any] | None = None,
) -> ReplayResult:
    validate_trace_schema(document, schema)
    required = {"schema_version", "kind", "model_version", "trace_id", "scenario", "provenance", "budget", "initial", "events", "expected", "recovery_observations"}
    _exact_keys(document, required, "replay trace")
    if document["schema_version"] != MODEL_VERSION or document["kind"] != "lifecycle-replay-trace" or document["model_version"] != MODEL_VERSION:
        raise TraceValidationError("unsupported replay trace version")
    if not isinstance(document["trace_id"], str) or not document["trace_id"]:
        raise TraceValidationError("trace id must be non-empty")
    if not isinstance(document["scenario"], str) or not _SCENARIO_RE.fullmatch(document["scenario"]):
        raise TraceValidationError("scenario is not an extensible lifecycle identifier")
    provenance = document["provenance"]
    provenance_keys = {"kind", "source_label", "evidence_id", "source_identity"}
    if provenance.get("kind") == "historical-observation":
        provenance_keys.add("artifact_sha256")
    _exact_keys(provenance, provenance_keys, "trace provenance")
    if provenance["kind"] not in {"historical-observation", "golden-scenario"}:
        raise TraceValidationError("invalid provenance kind")
    if provenance["kind"] == "historical-observation":
        if not isinstance(provenance["artifact_sha256"], str) or not re.fullmatch(r"^[0-9a-f]{64}$", provenance["artifact_sha256"]):
            raise TraceValidationError("historical observation requires immutable artifact_sha256")
    identity = provenance["source_identity"]
    _exact_keys(identity, {"repository", "commit", "tree", "profile", "module"}, "source identity")
    if not all(isinstance(identity[key], str) and identity[key] for key in identity) or not _HEX40.fullmatch(identity["commit"]) or not _HEX40.fullmatch(identity["tree"]):
        raise TraceValidationError("source identity is not immutable")
    _reject_authority_fields(document, "trace")
    if model is None:
        model_path = Path(__file__).resolve().parents[1] / "lifecycle" / "state-model-v1.json"
        model = load_json(model_path)
    validate_state_model(model)
    budget = document["budget"]
    _exact_keys(budget, {"max_events", "max_virtual_time_ns", "max_live_capabilities", "max_recovery_steps"}, "replay budget")
    if any(isinstance(budget[key], bool) or not isinstance(budget[key], int) or budget[key] < 1 for key in budget):
        raise TraceValidationError("replay budget must contain positive integers")
    if any(budget[key] > MODEL_BUDGETS[key] for key in MODEL_BUDGETS):
        raise TraceValidationError(f"trace budget exceeds authoritative model maxima: model={MODEL_BUDGETS}")
    initial = document["initial"]
    _exact_keys(initial, {"snapshot", "capability_catalog", "live_capabilities"}, "trace initial state")
    catalog: dict[str, AnchoredCapability] = {}
    for raw in initial["capability_catalog"]:
        cap = AnchoredCapability.from_mapping(raw)
        if cap.capability_id in catalog:
            raise TraceValidationError(f"duplicate capability catalog entry: {cap.capability_id}")
        catalog[cap.capability_id] = cap
    live: dict[str, str] = {}
    for raw in initial["live_capabilities"]:
        ownership = _ownership(raw)
        if ownership.capability not in catalog or ownership.capability in live:
            raise TraceValidationError("invalid initial live ownership")
        live[ownership.capability] = ownership.owner
    consumed_generations = {capability_id: {capability.identity.generation} for capability_id, capability in catalog.items() if capability_id in live}
    runtime = _ReplayRuntime(
        StateSnapshot.from_mapping(initial["snapshot"]),
        catalog,
        live,
        consumed_generations=consumed_generations,
        checkpoint_live=dict(live),
        checkpoint_matched=set(),
        checkpoint_gone=set(),
    )
    events = document["events"]
    if not events or len(events) > budget["max_events"]:
        raise TraceValidationError("event count exceeds replay budget")
    previous_time = -1
    for expected_seq, event in enumerate(events):
        if runtime.terminal_outcome is not None:
            raise TraceValidationError("terminal event must be unique and strictly last")
        _exact_keys(event, {"seq", "time_ns", "actor", "kind", "data", "state_after"}, "trace event")
        if isinstance(event["seq"], bool) or isinstance(event["time_ns"], bool) or event["seq"] != expected_seq or not isinstance(event["time_ns"], int) or event["time_ns"] < previous_time or event["time_ns"] > budget["max_virtual_time_ns"]:
            raise TraceValidationError("event sequence/time violates replay budget")
        previous_time = event["time_ns"]
        if event["actor"] not in _ACTORS or event["kind"] not in _EVENT_KINDS or not isinstance(event["data"], Mapping):
            raise TraceValidationError("unknown event actor/kind or non-object data")
        _validate_event_data(event["kind"], event["data"], catalog)
        apply_event(runtime, event, model)
        if len(runtime.live) > budget["max_live_capabilities"] or len(runtime.recovery_actions) > budget["max_recovery_steps"]:
            raise TraceValidationError("live capability or recovery-step budget exceeded")
    if events[-1]["kind"] != "terminal":
        raise TraceValidationError("terminal event must be last")
    unresolved = {item for item in runtime.obligations if item.startswith("fault:") or item in _UNRESOLVED_OBLIGATIONS}
    if runtime.terminal_outcome == "SUCCESS" and unresolved:
        raise TraceValidationError(f"successful terminal has unresolved obligations: {sorted(unresolved)}")
    expected = document["expected"]
    _exact_keys(expected, {"outcome", "stable_state", "journal_phase", "intent", "terminal_event_kind", "invariants", "live_capabilities"}, "trace expected result")
    final = runtime.snapshot
    if expected["terminal_event_kind"] != "terminal" or expected["outcome"] != runtime.terminal_outcome:
        raise TraceValidationError("expected terminal result differs from computed terminal")
    if (expected["stable_state"], expected["journal_phase"], expected["intent"]) != (final.stable_state.value, final.journal_phase.value, final.intent.value):
        raise TraceValidationError("expected terminal state differs from computed reducer state")
    if set(expected["invariants"]) != _INVARIANTS:
        raise TraceValidationError("trace must declare the complete executable invariant registry")
    satisfied = _invariants(runtime, document, budget)
    if not set(expected["invariants"]).issubset(satisfied):
        raise TraceValidationError(f"unsatisfied invariants: {sorted(set(expected['invariants']) - satisfied)}")
    expected_live = tuple(sorted((_ownership(item).capability, _ownership(item).owner) for item in expected["live_capabilities"]))
    actual_live = tuple(sorted(runtime.live.items()))
    if expected_live != actual_live:
        raise TraceValidationError(f"expected live capabilities differ from computed ownership: expected={expected_live}, actual={actual_live}")
    computed_observations = tuple(runtime.recovery_observations)
    recorded_observations: list[tuple[str, str, str]] = []
    for observation in document["recovery_observations"]:
        _exact_keys(observation, {"stable_state", "journal_phase", "action"}, "recovery observation")
        recorded_observations.append((observation["stable_state"], observation["journal_phase"], observation["action"]))
    if tuple(recorded_observations) != computed_observations:
        raise TraceValidationError(
            f"recovery observations differ from computed replay: recorded={recorded_observations}, computed={computed_observations}"
        )
    return ReplayResult(
        document["trace_id"],
        runtime.terminal_outcome,
        final,
        tuple(runtime.recovery_actions),
        computed_observations,
        actual_live,
        tuple(sorted(runtime.obligations)),
        tuple(sorted(satisfied)),
    )


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise TraceValidationError(f"{path} does not contain a JSON object")
    return value


def load_and_validate_golden_traces(root: Path) -> list[Path]:
    model = load_json(root / "lifecycle" / "state-model-v1.json")
    validate_state_model(model)
    paths = sorted((root / "tests" / "fixtures" / "lifecycle-traces" / "v1").glob("*.json"))
    if not paths:
        raise TraceValidationError("no lifecycle golden traces found")
    for path in paths:
        replay_trace(load_json(path), model)
    return paths


def recovery_matrix_pairs() -> Iterable[tuple[str, str, str]]:
    for (stable, phase), action in RECOVERY_MATRIX.items():
        yield stable.value, phase.value, action.value


def invariant_names() -> tuple[str, ...]:
    return tuple(sorted(_INVARIANTS))
