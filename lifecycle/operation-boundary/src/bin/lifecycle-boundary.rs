use darling_lifecycle_operation_boundary::state::{
    CapabilityKind, Event, IntentKind, JournalPhase, Outcome, RecoveryAction, Reducer, StableState,
    INVARIANT_REGISTRY, MODEL_MAX_EVENTS, MODEL_MAX_LIVE_CAPABILITIES, MODEL_MAX_RECOVERY_STEPS,
    MODEL_MAX_VIRTUAL_TIME_NS,
};
use darling_lifecycle_operation_boundary::{Boundary, NoFault, NoopObserver, RealClock};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::io::{self, Read};

#[derive(Deserialize)]
struct Request {
    op: String,
    #[serde(default)]
    policy: Option<Value>,
    #[serde(default)]
    trace: Option<Value>,
}

#[derive(Serialize)]
struct Response<'a> {
    status: &'a str,
    backend: &'a str,
    op: &'a str,
}

fn validate_policy(policy: &Value) -> Result<(), String> {
    let object = policy
        .as_object()
        .ok_or_else(|| "policy must be an object".to_string())?;
    require_keys(
        policy,
        &[
            "schema_version",
            "kind",
            "backend",
            "acceptance_scope",
            "capability_kinds",
            "operations",
            "rules",
        ],
        "policy",
    )?;
    if object.get("schema_version") != Some(&Value::from(1))
        || object.get("kind") != Some(&Value::from("lifecycle-operation-boundary-policy"))
    {
        return Err("unsupported lifecycle operation policy".to_string());
    }
    let backend = object
        .get("backend")
        .and_then(Value::as_object)
        .ok_or_else(|| "policy.backend must be an object".to_string())?;
    require_keys(
        &object["backend"],
        &["production", "test"],
        "policy.backend",
    )?;
    if backend.get("production") != Some(&Value::from("rust"))
        || backend.get("test")
            != Some(&Value::from(
                "same-rust-facade-with-scripted-clock-observer-faults",
            ))
    {
        return Err("policy backend is not Rust-authoritative".to_string());
    }
    let acceptance_scope = object
        .get("acceptance_scope")
        .and_then(Value::as_object)
        .ok_or_else(|| "policy.acceptance_scope must be an object".to_string())?;
    require_keys(
        &object["acceptance_scope"],
        &["boundary", "production_routing", "optimized_overhead"],
        "policy.acceptance_scope",
    )?;
    if acceptance_scope.get("boundary") != Some(&Value::from("infrastructure-only"))
        || acceptance_scope.get("production_routing")
            != Some(&Value::from("deferred_to_dar-4ush.7"))
        || acceptance_scope.get("optimized_overhead")
            != Some(&Value::from("deferred_to_dar-4ush.7"))
    {
        return Err("policy acceptance scope is not explicit".to_string());
    }
    let expected_capabilities = ["directory", "file", "pidfd", "lock", "exclusive_lease"];
    let expected_operations = [
        "clock_now",
        "anchor_directory",
        "open_child",
        "open_lock",
        "mkdir_child",
        "revalidate",
        "revalidate_metadata",
        "list_names",
        "read",
        "write",
        "fsync",
        "unlink_exact",
        "rename_exact",
        "pidfd_open",
        "pidfd_signal",
        "flock",
        "close",
        "stage_registered",
        "stage_published",
        "stage_cleanup",
        "quarantine",
    ];
    let strings = |key: &str| -> Result<Vec<&str>, String> {
        object
            .get(key)
            .and_then(Value::as_array)
            .ok_or_else(|| format!("policy.{key} must be an array"))?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .ok_or_else(|| format!("policy.{key} contains a non-string"))
            })
            .collect()
    };
    if strings("capability_kinds")?.as_slice() != expected_capabilities
        || strings("operations")?.as_slice() != expected_operations
    {
        return Err("policy operation/capability registry mismatch".to_string());
    }
    let rules = object
        .get("rules")
        .and_then(Value::as_object)
        .ok_or_else(|| "policy.rules must be an object".to_string())?;
    let expected_rule_keys = [
        "fd_relative_mutation",
        "retained_inode_revalidation",
        "registration_before_fallible_step",
        "bounded_iteration",
        "raw_syscalls_only_in_rust",
        "rust_owns_syscalls",
        "python_owns_syscalls",
        "path_allowed_only_for_anchor",
        "unvalidated_fd_allowed",
        "mutation_requires_retained_lease",
        "mutation_requires_exclusive_lease",
        "exact_child_identity_required",
        "capability_scope_branded",
        "cleanup_uses_quarantine_move",
        "quarantine_gc_requires_quiescence",
        "quarantine_gc_requires_external_scope",
        "quarantine_obligation_ownership",
        "stage_obligation_ownership",
        "obligation_queues_are_disjoint",
        "typed_journal_records",
        "operation_specific_capability_constructors",
        "stage_terminal_record",
        "stage_cleanup_terminal_record",
    ];
    require_keys(&object["rules"], &expected_rule_keys, "policy.rules")?;
    let expected_rules = [
        ("fd_relative_mutation", true),
        ("retained_inode_revalidation", true),
        ("registration_before_fallible_step", true),
        ("bounded_iteration", true),
        ("raw_syscalls_only_in_rust", true),
        ("rust_owns_syscalls", true),
        ("python_owns_syscalls", false),
        ("path_allowed_only_for_anchor", true),
        ("unvalidated_fd_allowed", false),
        ("mutation_requires_retained_lease", true),
        ("mutation_requires_exclusive_lease", true),
        ("exact_child_identity_required", true),
        ("capability_scope_branded", true),
        ("cleanup_uses_quarantine_move", true),
        ("quarantine_gc_requires_quiescence", true),
        ("quarantine_gc_requires_external_scope", true),
        ("quarantine_obligation_ownership", true),
        ("stage_obligation_ownership", true),
        ("obligation_queues_are_disjoint", true),
        ("typed_journal_records", true),
        ("operation_specific_capability_constructors", true),
        ("stage_terminal_record", true),
        ("stage_cleanup_terminal_record", true),
    ];
    for (key, expected) in expected_rules {
        if rules.get(key) != Some(&Value::from(expected)) {
            return Err(format!("policy rule {key} mismatch"));
        }
    }
    Ok(())
}

fn string<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("missing string field {key}"))
}

fn one_of<'a>(value: &'a Value, allowed: &[&str], label: &str) -> Result<&'a str, String> {
    let value = value
        .as_str()
        .ok_or_else(|| format!("{label} must be a string"))?;
    if allowed.contains(&value) {
        Ok(value)
    } else {
        Err(format!("{label} has unsupported value {value}"))
    }
}

fn capability_id(value: &Value, label: &str) -> Result<String, String> {
    let value = value
        .as_str()
        .ok_or_else(|| format!("{label} must be a string"))?;
    let valid = value.strip_prefix("cap.").is_some_and(|suffix| {
        !suffix.is_empty()
            && suffix.bytes().all(|byte| {
                byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"._-".contains(&byte)
            })
    });
    if !valid {
        return Err(format!("{label} is not a capability id"));
    }
    Ok(value.to_string())
}

fn require_keys(value: &Value, expected: &[&str], label: &str) -> Result<(), String> {
    let object = value
        .as_object()
        .ok_or_else(|| format!("{label} must be an object"))?;
    let actual: BTreeSet<&str> = object.keys().map(String::as_str).collect();
    let expected_set: BTreeSet<&str> = expected.iter().copied().collect();
    if actual != expected_set {
        return Err(format!("{label} keys differ"));
    }
    Ok(())
}

fn validate_trace_shape(trace: &Value) -> Result<(), String> {
    require_keys(
        trace,
        &[
            "schema_version",
            "kind",
            "model_version",
            "trace_id",
            "scenario",
            "provenance",
            "budget",
            "initial",
            "events",
            "expected",
            "recovery_observations",
        ],
        "trace",
    )?;
    if trace["schema_version"].as_u64() != Some(1)
        || trace["model_version"].as_u64() != Some(1)
        || trace["kind"].as_str() != Some("lifecycle-replay-trace")
    {
        return Err("unsupported replay trace schema".to_string());
    }
    if string(trace, "trace_id")?.is_empty() || string(trace, "scenario")?.is_empty() {
        return Err("trace identifiers must be non-empty".to_string());
    }
    let provenance_kind = trace["provenance"]["kind"]
        .as_str()
        .ok_or_else(|| "trace.provenance.kind must be a string".to_string())?;
    let provenance_keys = match provenance_kind {
        "historical-observation" => &[
            "kind",
            "source_label",
            "evidence_id",
            "source_identity",
            "artifact_sha256",
        ][..],
        "golden-scenario" => &["kind", "source_label", "evidence_id", "source_identity"][..],
        _ => return Err("invalid trace provenance kind".to_string()),
    };
    require_keys(&trace["provenance"], provenance_keys, "trace.provenance")?;
    require_keys(
        &trace["provenance"]["source_identity"],
        &["repository", "commit", "tree", "profile", "module"],
        "trace.provenance.source_identity",
    )?;
    require_keys(
        &trace["budget"],
        &[
            "max_events",
            "max_virtual_time_ns",
            "max_live_capabilities",
            "max_recovery_steps",
        ],
        "trace.budget",
    )?;
    require_keys(
        &trace["initial"],
        &["snapshot", "capability_catalog", "live_capabilities"],
        "trace.initial",
    )?;
    require_keys(
        &trace["initial"]["snapshot"],
        &["stable_state", "journal_phase", "intent"],
        "trace.initial.snapshot",
    )?;
    for capability in trace["initial"]["capability_catalog"]
        .as_array()
        .ok_or_else(|| "initial capability_catalog must be an array".to_string())?
    {
        require_keys(
            capability,
            &["capability_id", "kind", "identity"],
            "capability catalog entry",
        )?;
        require_keys(
            &capability["identity"],
            &["token", "generation"],
            "capability identity",
        )?;
        capability_id(&capability["capability_id"], "capability catalog id")?;
    }
    for ownership in trace["initial"]["live_capabilities"]
        .as_array()
        .ok_or_else(|| "initial live_capabilities must be an array".to_string())?
    {
        require_keys(ownership, &["capability", "owner"], "initial ownership")?;
        capability_id(&ownership["capability"], "initial ownership capability")?;
    }
    let events = trace["events"]
        .as_array()
        .ok_or_else(|| "trace.events must be an array".to_string())?;
    for event in events {
        require_keys(
            event,
            &["seq", "time_ns", "actor", "kind", "data", "state_after"],
            "trace event",
        )?;
        require_keys(
            &event["state_after"],
            &["stable_state", "journal_phase", "intent"],
            "event state_after",
        )?;
        let kind = string(event, "kind")?;
        let keys = match kind {
            "capability_acquired" => &["capability", "owner"][..],
            "capability_moved" => &["capability", "from", "to"][..],
            "capability_released" => &["capability", "owner", "reason"][..],
            "intent_declared" => &["intent", "transaction_id"][..],
            "barrier_entered" => &["barrier", "result"][..],
            "membership_snapshot" => &["authority", "members", "completeness"][..],
            "identity_revalidated" => &["capability", "result"][..],
            "signal_sent" => &["capability", "signal", "result"][..],
            "endpoint_transition" => &["endpoint", "operation", "result"][..],
            "member_observed" => &["capability", "origin"][..],
            "fault_injected" => &["phase", "fault"][..],
            "recovery" => &["action", "reason"][..],
            "terminal" => &["outcome"][..],
            _ => return Err(format!("unknown lifecycle event kind {kind}")),
        };
        require_keys(&event["data"], keys, &format!("{kind} data"))?;
        match kind {
            "capability_acquired" | "capability_moved" | "capability_released" => {
                capability_id(&event["data"]["capability"], "event capability")?;
            }
            "intent_declared" => {
                one_of(
                    &event["data"]["intent"],
                    &[
                        "CREATE_PREFIX",
                        "START_SESSION",
                        "REQUEST_SHUTDOWN",
                        "RELEASE_SESSION",
                        "RECREATE_PREFIX",
                        "RECOVER",
                    ],
                    "intent",
                )?;
            }
            "barrier_entered" => {
                one_of(
                    &event["data"]["barrier"],
                    &["THREADS_STOPPED", "SESSION_QUIESCED", "MEMBERSHIP_SNAPSHOT"],
                    "barrier",
                )?;
                one_of(
                    &event["data"]["result"],
                    &["ENTERED", "GONE", "TIMEOUT", "REJECTED"],
                    "barrier result",
                )?;
            }
            "membership_snapshot" => {
                one_of(
                    &event["data"]["authority"],
                    &["SESSION_LEDGER", "DELEGATED_CGROUP", "PROC_TASK_CHILDREN"],
                    "membership authority",
                )?;
                one_of(
                    &event["data"]["completeness"],
                    &["COMPLETE", "CLOSED", "ABORTED_GONE", "OVERFLOW", "TIMEOUT"],
                    "membership completeness",
                )?;
                for member in event["data"]["members"]
                    .as_array()
                    .ok_or_else(|| "membership members must be an array".to_string())?
                {
                    capability_id(member, "membership capability")?;
                }
            }
            "identity_revalidated" => {
                capability_id(&event["data"]["capability"], "identity capability")?;
                one_of(
                    &event["data"]["result"],
                    &["MATCH", "MISMATCH", "GONE"],
                    "identity result",
                )?;
            }
            "signal_sent" => {
                capability_id(&event["data"]["capability"], "signal capability")?;
                one_of(
                    &event["data"]["signal"],
                    &["TERM", "KILL", "STOP", "CONT"],
                    "signal",
                )?;
                one_of(
                    &event["data"]["result"],
                    &["SENT", "GONE", "REJECTED", "DEADLINE"],
                    "signal result",
                )?;
            }
            "endpoint_transition" => {
                one_of(
                    &event["data"]["operation"],
                    &["CLOSE", "UNLINK", "REVALIDATE"],
                    "endpoint operation",
                )?;
                one_of(
                    &event["data"]["result"],
                    &["REMOVED", "ALREADY_GONE", "MISMATCH", "REJECTED"],
                    "endpoint result",
                )?;
            }
            "member_observed" => {
                capability_id(&event["data"]["capability"], "observed member")?;
                one_of(
                    &event["data"]["origin"],
                    &["STARTUP_LEDGER", "SNAPSHOT", "LATE_FORK", "REPLACEMENT"],
                    "member origin",
                )?;
            }
            "fault_injected" => {
                one_of(
                    &event["data"]["phase"],
                    &[
                        "PREPARE", "PUBLISH", "CLEANUP", "COMMIT", "BARRIER", "SIGNAL",
                    ],
                    "fault phase",
                )?;
                one_of(
                    &event["data"]["fault"],
                    &[
                        "SIGINT",
                        "TIMEOUT",
                        "ROOT_EXIT",
                        "PID_REUSE",
                        "LATE_FORK",
                        "LEASE_HOLDER",
                        "CLOSE_RANGE",
                    ],
                    "fault",
                )?;
            }
            "recovery" => {
                one_of(
                    &event["data"]["action"],
                    &[
                        "INITIALIZE",
                        "NOOP",
                        "ROLLBACK_UNINITIALIZED",
                        "ROLLBACK_READY",
                        "ROLLBACK_RUNNING",
                        "ROLLBACK_STOPPED",
                        "COMPLETE_PUBLISH",
                        "COMPLETE_CLEANUP",
                        "DRAIN_TO_READY",
                        "CONTINUE_DRAIN",
                        "FAIL_CLOSED",
                        "QUARANTINE",
                    ],
                    "recovery action",
                )?;
            }
            "terminal" => {
                one_of(
                    &event["data"]["outcome"],
                    &["SUCCESS", "FAIL_CLOSED", "TIMEOUT", "RECOVERED"],
                    "terminal outcome",
                )?;
            }
            _ => {}
        }
    }
    require_keys(
        &trace["expected"],
        &[
            "outcome",
            "stable_state",
            "journal_phase",
            "intent",
            "terminal_event_kind",
            "invariants",
            "live_capabilities",
        ],
        "trace.expected",
    )?;
    for observation in trace["recovery_observations"]
        .as_array()
        .ok_or_else(|| "recovery_observations must be an array".to_string())?
    {
        require_keys(
            observation,
            &["stable_state", "journal_phase", "action"],
            "recovery observation",
        )?;
    }
    Ok(())
}

fn stable(value: &Value) -> Result<StableState, String> {
    match value.as_str() {
        Some("UNINITIALIZED") => Ok(StableState::Uninitialized),
        Some("READY") => Ok(StableState::Ready),
        Some("RUNNING") => Ok(StableState::Running),
        Some("DRAINING") => Ok(StableState::Draining),
        Some("STOPPED") => Ok(StableState::Stopped),
        Some("CORRUPT") => Ok(StableState::Corrupt),
        _ => Err("invalid stable state".to_string()),
    }
}

fn journal(value: &Value) -> Result<JournalPhase, String> {
    match value.as_str() {
        Some("NONE") => Ok(JournalPhase::None),
        Some("PREPARE") => Ok(JournalPhase::Prepare),
        Some("PUBLISH") => Ok(JournalPhase::Publish),
        Some("CLEANUP") => Ok(JournalPhase::Cleanup),
        Some("COMMIT") => Ok(JournalPhase::Commit),
        Some("ABORT") => Ok(JournalPhase::Abort),
        _ => Err("invalid journal phase".to_string()),
    }
}

fn intent(value: &Value) -> Result<IntentKind, String> {
    match value.as_str() {
        Some("NONE") => Ok(IntentKind::None),
        Some("CREATE_PREFIX") => Ok(IntentKind::CreatePrefix),
        Some("START_SESSION") => Ok(IntentKind::StartSession),
        Some("REQUEST_SHUTDOWN") => Ok(IntentKind::RequestShutdown),
        Some("RELEASE_SESSION") => Ok(IntentKind::ReleaseSession),
        Some("RECREATE_PREFIX") => Ok(IntentKind::RecreatePrefix),
        Some("RECOVER") => Ok(IntentKind::Recover),
        _ => Err("invalid lifecycle intent".to_string()),
    }
}

fn capability_kind(value: &Value) -> Result<CapabilityKind, String> {
    match value.as_str() {
        Some("PREFIX_DIRECTORY") => Ok(CapabilityKind::PrefixDirectory),
        Some("SESSION_ROOT_PIDFD") => Ok(CapabilityKind::SessionRootPidfd),
        Some("SESSION_MEMBER_PIDFD") => Ok(CapabilityKind::SessionMemberPidfd),
        Some("SHARED_SESSION_LEASE") => Ok(CapabilityKind::SharedSessionLease),
        Some("RUNTIME_ENDPOINT") => Ok(CapabilityKind::RuntimeEndpoint),
        Some("JOURNAL_RECORD") => Ok(CapabilityKind::JournalRecord),
        _ => Err("invalid capability kind".to_string()),
    }
}

fn recovery_action(value: &Value) -> Result<RecoveryAction, String> {
    match value.as_str() {
        Some("INITIALIZE") => Ok(RecoveryAction::Initialize),
        Some("NOOP") => Ok(RecoveryAction::Noop),
        Some("ROLLBACK_UNINITIALIZED") => Ok(RecoveryAction::RollbackUninitialized),
        Some("ROLLBACK_READY") => Ok(RecoveryAction::RollbackReady),
        Some("ROLLBACK_RUNNING") => Ok(RecoveryAction::RollbackRunning),
        Some("ROLLBACK_STOPPED") => Ok(RecoveryAction::RollbackStopped),
        Some("COMPLETE_PUBLISH") => Ok(RecoveryAction::CompletePublish),
        Some("COMPLETE_CLEANUP") => Ok(RecoveryAction::CompleteCleanup),
        Some("DRAIN_TO_READY") => Ok(RecoveryAction::DrainToReady),
        Some("CONTINUE_DRAIN") => Ok(RecoveryAction::ContinueDrain),
        Some("FAIL_CLOSED") => Ok(RecoveryAction::FailClosed),
        Some("QUARANTINE") => Ok(RecoveryAction::Quarantine),
        _ => Err("invalid recovery action".to_string()),
    }
}

fn outcome_name(outcome: Outcome) -> &'static str {
    match outcome {
        Outcome::Success => "SUCCESS",
        Outcome::FailClosed => "FAIL_CLOSED",
        Outcome::Timeout => "TIMEOUT",
        Outcome::Recovered => "RECOVERED",
    }
}

fn stable_name(state: StableState) -> &'static str {
    match state {
        StableState::Uninitialized => "UNINITIALIZED",
        StableState::Ready => "READY",
        StableState::Running => "RUNNING",
        StableState::Draining => "DRAINING",
        StableState::Stopped => "STOPPED",
        StableState::Corrupt => "CORRUPT",
    }
}

fn journal_name(phase: JournalPhase) -> &'static str {
    match phase {
        JournalPhase::None => "NONE",
        JournalPhase::Prepare => "PREPARE",
        JournalPhase::Publish => "PUBLISH",
        JournalPhase::Cleanup => "CLEANUP",
        JournalPhase::Commit => "COMMIT",
        JournalPhase::Abort => "ABORT",
    }
}

fn intent_name(kind: IntentKind) -> &'static str {
    match kind {
        IntentKind::None => "NONE",
        IntentKind::CreatePrefix => "CREATE_PREFIX",
        IntentKind::StartSession => "START_SESSION",
        IntentKind::RequestShutdown => "REQUEST_SHUTDOWN",
        IntentKind::ReleaseSession => "RELEASE_SESSION",
        IntentKind::RecreatePrefix => "RECREATE_PREFIX",
        IntentKind::Recover => "RECOVER",
    }
}

fn recovery_name(action: RecoveryAction) -> &'static str {
    match action {
        RecoveryAction::Initialize => "INITIALIZE",
        RecoveryAction::Noop => "NOOP",
        RecoveryAction::RollbackUninitialized => "ROLLBACK_UNINITIALIZED",
        RecoveryAction::RollbackReady => "ROLLBACK_READY",
        RecoveryAction::RollbackRunning => "ROLLBACK_RUNNING",
        RecoveryAction::RollbackStopped => "ROLLBACK_STOPPED",
        RecoveryAction::CompletePublish => "COMPLETE_PUBLISH",
        RecoveryAction::CompleteCleanup => "COMPLETE_CLEANUP",
        RecoveryAction::DrainToReady => "DRAIN_TO_READY",
        RecoveryAction::ContinueDrain => "CONTINUE_DRAIN",
        RecoveryAction::FailClosed => "FAIL_CLOSED",
        RecoveryAction::Quarantine => "QUARANTINE",
    }
}

fn snapshot_json(snapshot: &darling_lifecycle_operation_boundary::state::StateSnapshot) -> Value {
    json!({
        "stable_state": stable_name(snapshot.stable),
        "journal_phase": journal_name(snapshot.journal),
        "intent": intent_name(snapshot.intent),
    })
}

fn check_snapshot(
    recorded: &Value,
    computed: &darling_lifecycle_operation_boundary::state::StateSnapshot,
) -> Result<(), String> {
    if recorded != &snapshot_json(computed) {
        return Err(format!(
            "event state_after differs: recorded={recorded}, computed={}",
            snapshot_json(computed)
        ));
    }
    Ok(())
}

fn map_event(
    kind: &str,
    data: &Value,
    catalog: &BTreeMap<String, (CapabilityKind, u64)>,
) -> Result<Option<Event>, String> {
    let data_object = data
        .as_object()
        .ok_or_else(|| "event data must be an object".to_string())?;
    let capability = |key: &str| -> Result<String, String> { Ok(string(data, key)?.to_string()) };
    match kind {
        "capability_acquired" => {
            let id = capability("capability")?;
            let (kind, generation) = catalog
                .get(&id)
                .ok_or_else(|| "acquire references unknown capability".to_string())?;
            Ok(Some(Event::Acquire {
                id,
                kind: *kind,
                generation: *generation,
                owner: string(data, "owner")?.to_string(),
            }))
        }
        "capability_moved" => Ok(Some(Event::Move {
            id: capability("capability")?,
            from: string(data, "from")?.to_string(),
            to: string(data, "to")?.to_string(),
        })),
        "capability_released" => Ok(Some(Event::Release {
            id: capability("capability")?,
            owner: string(data, "owner")?.to_string(),
        })),
        "intent_declared" => Ok(Some(Event::Intent {
            intent: intent(&data_object["intent"])?,
            transaction: string(data, "transaction_id")?.to_string(),
        })),
        "barrier_entered" => {
            if string(data, "result")? == "ENTERED" {
                Ok(Some(Event::BarrierEntered))
            } else {
                Ok(None)
            }
        }
        "membership_snapshot" => Ok(Some(Event::Membership {
            members: data_object
                .get("members")
                .and_then(Value::as_array)
                .ok_or_else(|| "membership members must be an array".to_string())?
                .iter()
                .map(|member| {
                    member
                        .as_str()
                        .map(ToOwned::to_owned)
                        .ok_or_else(|| "membership member must be a string".to_string())
                })
                .collect::<Result<Vec<_>, _>>()?,
            completeness: string(data, "completeness")?.to_string(),
        })),
        "identity_revalidated" => Ok(Some(Event::Identity {
            id: capability("capability")?,
            matches: string(data, "result")? == "MATCH",
        })),
        "signal_sent" => Ok(Some(Event::Signal {
            id: capability("capability")?,
        })),
        "endpoint_transition" => Ok(Some(Event::Endpoint {
            endpoint: string(data, "endpoint")?.to_string(),
            operation: string(data, "operation")?.to_string(),
            result: string(data, "result")?.to_string(),
        })),
        "member_observed" => Ok(Some(Event::MemberObserved {
            id: capability("capability")?,
            origin: string(data, "origin")?.to_string(),
        })),
        "fault_injected" => Ok(Some(Event::Fault {
            name: string(data, "fault")?.to_string(),
        })),
        "recovery" => Ok(Some(Event::Recovery(recovery_action(
            &data_object["action"],
        )?))),
        "terminal" => Ok(Some(Event::Terminal(match string(data, "outcome")? {
            "SUCCESS" => Outcome::Success,
            "FAIL_CLOSED" => Outcome::FailClosed,
            "TIMEOUT" => Outcome::Timeout,
            "RECOVERED" => Outcome::Recovered,
            _ => return Err("invalid terminal outcome".to_string()),
        }))),
        _ => Err(format!("unsupported lifecycle event kind {kind}")),
    }
}

fn replay_trace(trace: &Value) -> Result<Value, String> {
    validate_trace_shape(trace)?;
    let object = trace
        .as_object()
        .ok_or_else(|| "trace must be an object".to_string())?;
    let initial = object
        .get("initial")
        .and_then(Value::as_object)
        .ok_or_else(|| "trace.initial must be an object".to_string())?;
    let initial_snapshot = initial
        .get("snapshot")
        .and_then(Value::as_object)
        .ok_or_else(|| "trace.initial.snapshot must be an object".to_string())?;
    let initial_stable = stable(&initial_snapshot["stable_state"])?;
    if journal(&initial_snapshot["journal_phase"])? != JournalPhase::None
        || intent(&initial_snapshot["intent"])? != IntentKind::None
    {
        return Err("Rust replay requires an idle initial journal/intent".to_string());
    }
    let mut reducer = Reducer::new(initial_stable);
    let mut catalog = BTreeMap::new();
    for capability in initial
        .get("capability_catalog")
        .and_then(Value::as_array)
        .ok_or_else(|| "capability_catalog must be an array".to_string())?
    {
        let id = string(capability, "capability_id")?.to_string();
        let kind = capability_kind(&capability["kind"])?;
        let generation = capability["identity"]["generation"]
            .as_u64()
            .ok_or_else(|| "capability generation must be an integer".to_string())?;
        reducer
            .register_capability(id.clone(), kind, generation)
            .map_err(|error| error.to_string())?;
        catalog.insert(id, (kind, generation));
    }
    for ownership in initial
        .get("live_capabilities")
        .and_then(Value::as_array)
        .ok_or_else(|| "live_capabilities must be an array".to_string())?
    {
        reducer
            .seed_ownership(
                string(ownership, "capability")?,
                string(ownership, "owner")?,
            )
            .map_err(|error| error.to_string())?;
    }
    let budget = object
        .get("budget")
        .and_then(Value::as_object)
        .ok_or_else(|| "trace.budget must be an object".to_string())?;
    let max_events = budget["max_events"]
        .as_u64()
        .ok_or_else(|| "max_events must be an integer".to_string())? as usize;
    let max_virtual_time_ns = budget["max_virtual_time_ns"]
        .as_u64()
        .ok_or_else(|| "max_virtual_time_ns must be an integer".to_string())?;
    let max_live_capabilities = budget["max_live_capabilities"]
        .as_u64()
        .ok_or_else(|| "max_live_capabilities must be an integer".to_string())?
        as usize;
    let max_recovery_steps = budget["max_recovery_steps"]
        .as_u64()
        .ok_or_else(|| "max_recovery_steps must be an integer".to_string())?
        as usize;
    if max_events > MODEL_MAX_EVENTS
        || max_virtual_time_ns > MODEL_MAX_VIRTUAL_TIME_NS
        || max_live_capabilities > MODEL_MAX_LIVE_CAPABILITIES
        || max_recovery_steps > MODEL_MAX_RECOVERY_STEPS
    {
        return Err("trace budget exceeds authoritative lifecycle model maxima".to_string());
    }
    let events = object
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| "trace.events must be an array".to_string())?;
    if events.len() < 2 || events.len() > max_events {
        return Err("event count exceeds replay budget".to_string());
    }
    let mut previous_time = 0u64;
    let mut terminal_seen = false;
    for (index, event) in events.iter().enumerate() {
        let event_object = event
            .as_object()
            .ok_or_else(|| "event must be an object".to_string())?;
        if terminal_seen {
            return Err("terminal event must be unique and strictly last".to_string());
        }
        if event_object["seq"].as_u64() != Some(index as u64) {
            return Err("event sequence is not contiguous".to_string());
        }
        if !matches!(
            string(event, "actor")?,
            "launcher" | "controller" | "launchd" | "member" | "observer" | "recovery"
        ) {
            return Err("unknown lifecycle event actor".to_string());
        }
        let time_ns = event_object["time_ns"]
            .as_u64()
            .ok_or_else(|| "event time must be an integer".to_string())?;
        if time_ns < previous_time || time_ns > max_virtual_time_ns {
            return Err("event time violates replay budget".to_string());
        }
        previous_time = time_ns;
        let kind = string(event, "kind")?;
        let mapped = map_event(kind, &event_object["data"], &catalog)?;
        if let Some(mapped) = mapped {
            reducer.apply(mapped).map_err(|error| error.to_string())?;
        }
        check_snapshot(&event_object["state_after"], reducer.snapshot())?;
        if kind == "terminal" {
            terminal_seen = true;
        }
        if reducer.live().len() > max_live_capabilities
            || reducer.observations().len() > max_recovery_steps
        {
            return Err("live capability or recovery budget exceeded".to_string());
        }
    }
    if !terminal_seen {
        return Err("terminal event must be last".to_string());
    }
    let outcome = reducer
        .terminal()
        .ok_or_else(|| "trace has no terminal".to_string())?;
    let expected = object
        .get("expected")
        .and_then(Value::as_object)
        .ok_or_else(|| "trace.expected must be an object".to_string())?;
    if expected["outcome"].as_str() != Some(outcome_name(outcome))
        || expected["terminal_event_kind"].as_str() != Some("terminal")
        || expected["stable_state"].as_str() != Some(stable_name(reducer.snapshot().stable))
        || expected["journal_phase"].as_str() != Some(journal_name(reducer.snapshot().journal))
        || expected["intent"].as_str() != Some(intent_name(reducer.snapshot().intent))
    {
        return Err("expected terminal result differs from Rust replay".to_string());
    }
    let actual_live: BTreeMap<String, String> = reducer
        .live()
        .iter()
        .map(|(id, owner)| (id.clone(), owner.clone()))
        .collect();
    let mut expected_live = BTreeMap::new();
    for ownership in expected["live_capabilities"]
        .as_array()
        .ok_or_else(|| "expected live_capabilities must be an array".to_string())?
    {
        expected_live.insert(
            string(ownership, "capability")?.to_string(),
            string(ownership, "owner")?.to_string(),
        );
    }
    if actual_live != expected_live {
        return Err("expected live capabilities differ from Rust replay".to_string());
    }
    let satisfied = reducer.invariant_names(
        events.len(),
        previous_time,
        max_events,
        max_virtual_time_ns,
        max_live_capabilities,
        max_recovery_steps,
    );
    let expected_invariants: BTreeSet<String> = expected["invariants"]
        .as_array()
        .ok_or_else(|| "expected invariants must be an array".to_string())?
        .iter()
        .map(|value| {
            value
                .as_str()
                .map(ToOwned::to_owned)
                .ok_or_else(|| "invariant must be a string".to_string())
        })
        .collect::<Result<BTreeSet<_>, _>>()?;
    let invariant_registry: BTreeSet<String> = INVARIANT_REGISTRY
        .iter()
        .copied()
        .map(str::to_string)
        .collect();
    if expected_invariants != invariant_registry {
        return Err("trace invariant registry is incomplete or unexpected".to_string());
    }
    if !expected_invariants.is_subset(&satisfied) {
        return Err(format!(
            "Rust replay did not satisfy invariants: {:?}",
            expected_invariants
                .difference(&satisfied)
                .collect::<Vec<_>>()
        ));
    }
    let observations: Vec<Value> = reducer
        .observations()
        .iter()
        .map(|observation| {
            json!({
                "stable_state": stable_name(observation.stable),
                "journal_phase": journal_name(observation.journal),
                "action": recovery_name(observation.action),
            })
        })
        .collect();
    let recorded_observations = object["recovery_observations"]
        .as_array()
        .ok_or_else(|| "recovery_observations must be an array".to_string())?;
    if recorded_observations != &observations {
        return Err("recorded recovery observations differ from Rust replay".to_string());
    }
    let obligations: Vec<_> = reducer.obligations().iter().cloned().collect();
    let live_capabilities: Vec<Value> = reducer
        .live()
        .iter()
        .map(|(capability, owner)| json!({"capability": capability, "owner": owner}))
        .collect();
    Ok(json!({
        "trace_id": string(trace, "trace_id")?,
        "outcome": outcome_name(outcome),
        "final_snapshot": snapshot_json(reducer.snapshot()),
        "recovery_observations": observations,
        "live_capabilities": live_capabilities,
        "obligations": obligations,
        "satisfied_invariants": satisfied,
    }))
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = String::new();
    io::stdin().read_to_string(&mut input)?;
    let request: Request = serde_json::from_str(&input)?;
    match request.op.as_str() {
        "self_check" => {
            let policy = request
                .policy
                .as_ref()
                .ok_or_else(|| "self_check requires lifecycle operation policy".to_string())?;
            validate_policy(policy).map_err(io::Error::other)?;
            let _boundary: Boundary<RealClock, NoopObserver, NoFault> = Boundary::production();
            println!(
                "{}",
                serde_json::to_string(&Response {
                    status: "ok",
                    backend: "rust",
                    op: "self_check",
                })?
            );
        }
        "replay_trace" => {
            let trace = request
                .trace
                .as_ref()
                .ok_or_else(|| "replay_trace requires trace".to_string())?;
            println!("{}", serde_json::to_string(&replay_trace(trace)?)?);
        }
        _ => {
            let response = Response {
                status: "error",
                backend: "rust",
                op: &request.op,
            };
            println!("{}", serde_json::to_string(&response)?);
            std::process::exit(2);
        }
    }
    Ok(())
}
