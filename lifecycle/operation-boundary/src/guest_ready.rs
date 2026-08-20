//! Closed Rust oracle for the trace-driven guest-ready `.5.2` lane.
//!
//! Linux and Darling facts are collected by an external bounded transport,
//! but this module is the only place that interprets them.  In particular,
//! the transport cannot choose an event order, terminal state, recovery
//! policy, or admissible witness for one of the six accepted traces.

use crate::fuzz::{canonical_trace_spec, hex_encode, safe_replay};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;

pub const MAX_GUEST_OBSERVATION_BYTES: usize = 32 * 1024;
pub const MAX_GUEST_EVENTS: usize = 16;
pub const MAX_GUEST_ENDPOINTS: usize = 8;
pub const MAX_GUEST_OUTPUT_BYTES: u64 = 1024 * 1024;
pub const MAX_GUEST_PROCESSES: u32 = 4096;
pub const MAX_GUEST_FDS: u32 = 4096;
pub const MAX_GUEST_ELAPSED_NS: u64 = 180_000_000_000;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub nlink: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub starttime: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct EndpointObservation {
    pub name: String,
    pub identity: FileIdentity,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GuestBudgets {
    pub elapsed_ns: u64,
    pub output_bytes: u64,
    pub processes_observed: u32,
    pub max_fds_observed: u32,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum GuestPhase {
    SourceBound,
    PrefixBound,
    BootRequested,
    GuestReady,
    RpcCompleted,
    SignalAttempted,
    SignalRejected,
    RecoveryCompleted,
    ShutdownRequested,
    RootGone,
    CleanupVerified,
    HolderObserved,
    DeadlineExpired,
    RetiredRootBound,
    ReuseBootRequested,
    ReplacementRootBound,
    OldPidfdRefused,
    SnapshotTaken,
    LateForkObserved,
    LateForkGone,
    RootExitObserved,
    SnapshotRefused,
    ForensicCleanup,
    Terminal,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum GuestTerminal {
    Recovered,
    Timeout,
    FailClosed,
}

impl GuestTerminal {
    fn as_str(self) -> &'static str {
        match self {
            Self::Recovered => "RECOVERED",
            Self::Timeout => "TIMEOUT",
            Self::FailClosed => "FAIL_CLOSED",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct GuestObservationCommon {
    pub trace_id: String,
    pub phases: Vec<GuestPhase>,
    pub terminal: GuestTerminal,
    pub unresolved_obligations: Vec<String>,
    pub source_identity_sha256: String,
    pub runtime_identity_sha256: String,
    pub verifier_semantic_closure_sha256: String,
    pub prefix_generation: u64,
    pub prefix: FileIdentity,
    pub session_root: ProcessIdentity,
    pub root_pidfd_retained: bool,
    pub endpoints: Vec<EndpointObservation>,
    pub route: String,
    pub routing: String,
    pub budgets: GuestBudgets,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "mode", deny_unknown_fields)]
pub enum GuestReadyObservation {
    #[serde(rename = "signal-gone")]
    SignalGone {
        common: GuestObservationCommon,
        post_shutdown_signal: String,
        cleanup_complete: bool,
    },
    #[serde(rename = "signal-rejected")]
    SignalRejected {
        common: GuestObservationCommon,
        rejection_errno: String,
        root_still_alive: bool,
        recovery_completed: bool,
    },
    #[serde(rename = "retained-holder-timeout")]
    RetainedHolderTimeout {
        common: GuestObservationCommon,
        deadline: String,
        holder_retained: bool,
        recovery_completed: bool,
    },
    #[serde(rename = "pid-reuse")]
    PidReuse {
        common: GuestObservationCommon,
        original: ProcessIdentity,
        replacement: ProcessIdentity,
        retired_pidfd_signal: String,
        replacement_alive: bool,
        same_prefix: bool,
    },
    #[serde(rename = "late-fork")]
    LateFork {
        common: GuestObservationCommon,
        late_child: ProcessIdentity,
        observed_after_snapshot: bool,
        post_shutdown: String,
        cleanup_complete: bool,
    },
    #[serde(rename = "root-exit-before-snapshot")]
    RootExitBeforeSnapshot {
        common: GuestObservationCommon,
        root_state: String,
        snapshot_attempted: bool,
        children_traversed: bool,
        forensic_cleanup_complete: bool,
    },
}

impl GuestReadyObservation {
    fn common(&self) -> &GuestObservationCommon {
        match self {
            Self::SignalGone { common, .. }
            | Self::SignalRejected { common, .. }
            | Self::RetainedHolderTimeout { common, .. }
            | Self::PidReuse { common, .. }
            | Self::LateFork { common, .. }
            | Self::RootExitBeforeSnapshot { common, .. } => common,
        }
    }

    fn mode(&self) -> &'static str {
        match self {
            Self::SignalGone { .. } => "signal-gone",
            Self::SignalRejected { .. } => "signal-rejected",
            Self::RetainedHolderTimeout { .. } => "retained-holder-timeout",
            Self::PidReuse { .. } => "pid-reuse",
            Self::LateFork { .. } => "late-fork",
            Self::RootExitBeforeSnapshot { .. } => "root-exit-before-snapshot",
        }
    }
}

fn expected_phases(mode: &str) -> &'static [GuestPhase] {
    use GuestPhase::*;
    match mode {
        "signal-gone" => &[
            SourceBound,
            PrefixBound,
            BootRequested,
            GuestReady,
            RpcCompleted,
            ShutdownRequested,
            RootGone,
            CleanupVerified,
            Terminal,
        ],
        "signal-rejected" => &[
            SourceBound,
            PrefixBound,
            BootRequested,
            GuestReady,
            SignalAttempted,
            SignalRejected,
            RecoveryCompleted,
            Terminal,
        ],
        "retained-holder-timeout" => &[
            SourceBound,
            PrefixBound,
            BootRequested,
            GuestReady,
            HolderObserved,
            DeadlineExpired,
            ShutdownRequested,
            CleanupVerified,
            Terminal,
        ],
        "pid-reuse" => &[
            SourceBound,
            PrefixBound,
            RetiredRootBound,
            ReuseBootRequested,
            ReplacementRootBound,
            OldPidfdRefused,
            Terminal,
        ],
        "late-fork" => &[
            SourceBound,
            PrefixBound,
            BootRequested,
            GuestReady,
            SnapshotTaken,
            LateForkObserved,
            ShutdownRequested,
            LateForkGone,
            CleanupVerified,
            Terminal,
        ],
        "root-exit-before-snapshot" => &[
            SourceBound,
            PrefixBound,
            BootRequested,
            GuestReady,
            RootExitObserved,
            SnapshotRefused,
            ForensicCleanup,
            Terminal,
        ],
        _ => &[],
    }
}

pub fn guest_ready_template(input: &[u8], trace_id: &str) -> Result<Value, String> {
    let (canonical_hex, mode, _reducer_events) = canonical_trace_spec(trace_id)
        .ok_or_else(|| format!("unknown canonical guest trace {trace_id}"))?;
    if hex_encode(input) != canonical_hex {
        return Err("bytecode does not match canonical trace identity".to_string());
    }
    let replay = safe_replay(input);
    if replay.status != "ACCEPTED" {
        return Err(format!(
            "guest template attached to {} trace",
            replay.status
        ));
    }
    Ok(json!({
        "trace_id": trace_id,
        "mode": mode,
        "phases": expected_phases(mode),
        "terminal": replay.terminal,
        "oracle_status": replay.status,
    }))
}

fn digest_is_canonical(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn identity_is_valid(identity: &FileIdentity) -> bool {
    identity.device > 0 && identity.inode > 0 && identity.nlink > 0
}

fn endpoint_type_is_valid(endpoint: &EndpointObservation) -> bool {
    let file_type = endpoint.identity.mode & libc::S_IFMT;
    match endpoint.name.as_str() {
        ".init.pid" => file_type == libc::S_IFREG,
        ".darlingserver.sock"
        | ".lc-v1.sock"
        | "var/run/shellspawn.sock"
        | "var/tmp/launchd/sock" => file_type == libc::S_IFSOCK,
        _ => false,
    }
}

fn common_is_valid(common: &GuestObservationCommon) -> bool {
    let names: BTreeSet<_> = common
        .endpoints
        .iter()
        .map(|item| item.name.as_str())
        .collect();
    let required: BTreeSet<_> = [
        ".init.pid",
        ".darlingserver.sock",
        "var/run/shellspawn.sock",
    ]
    .into_iter()
    .collect();
    common.phases.len() <= MAX_GUEST_EVENTS
        && common.endpoints.len() <= MAX_GUEST_ENDPOINTS
        && names.len() == common.endpoints.len()
        && common.endpoints.iter().all(|item| {
            !item.name.is_empty()
                && identity_is_valid(&item.identity)
                && endpoint_type_is_valid(item)
                && item.identity.uid == common.prefix.uid
        })
        && required.is_subset(&names)
        && common.prefix.mode & libc::S_IFMT == libc::S_IFDIR
        && common.unresolved_obligations.is_empty()
        && digest_is_canonical(&common.source_identity_sha256)
        && digest_is_canonical(&common.runtime_identity_sha256)
        && common.verifier_semantic_closure_sha256 == env!("LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_SHA256")
        && common.prefix_generation > 0
        && identity_is_valid(&common.prefix)
        && common.session_root.pid > 1
        && common.session_root.starttime > 0
        && common.root_pidfd_retained
        && common.route == "OFF"
        && common.routing == "DEFERRED"
        && common.budgets.elapsed_ns > 0
        && common.budgets.elapsed_ns <= MAX_GUEST_ELAPSED_NS
        && common.budgets.output_bytes <= MAX_GUEST_OUTPUT_BYTES
        && common.budgets.processes_observed > 0
        && common.budgets.processes_observed <= MAX_GUEST_PROCESSES
        && common.budgets.max_fds_observed <= MAX_GUEST_FDS
}

pub fn verify_guest_ready_observation(
    input: &[u8],
    observation: &GuestReadyObservation,
    expected_source_identity_sha256: &str,
    expected_runtime_identity_sha256: &str,
) -> Result<Value, String> {
    let common = observation.common();
    let mode = observation.mode();
    let (canonical_hex, expected_mode, _reducer_events) = canonical_trace_spec(&common.trace_id)
        .ok_or_else(|| format!("unknown canonical guest trace {}", common.trace_id))?;
    if hex_encode(input) != canonical_hex {
        return Err("bytecode does not match canonical trace identity".to_string());
    }
    if mode != expected_mode {
        return Err("guest mode does not match canonical trace identity".to_string());
    }
    if common.phases != expected_phases(mode) {
        return Err("guest production phase sequence is not canonical".to_string());
    }
    if !common_is_valid(common) {
        return Err("guest observation omitted a bounded safety fact".to_string());
    }
    if !digest_is_canonical(expected_source_identity_sha256)
        || !digest_is_canonical(expected_runtime_identity_sha256)
        || common.source_identity_sha256 != expected_source_identity_sha256
        || common.runtime_identity_sha256 != expected_runtime_identity_sha256
    {
        return Err("guest observation identity binding mismatch".to_string());
    }
    let replay = safe_replay(input);
    if replay.status != "ACCEPTED" {
        return Err(format!(
            "guest observation attached to {} trace",
            replay.status
        ));
    }
    if replay.terminal.as_deref() != Some(common.terminal.as_str()) {
        return Err("guest terminal differs from Rust reducer".to_string());
    }
    match observation {
        GuestReadyObservation::SignalGone {
            post_shutdown_signal,
            cleanup_complete,
            ..
        } if post_shutdown_signal == "ESRCH" && *cleanup_complete => {}
        GuestReadyObservation::SignalRejected {
            rejection_errno,
            root_still_alive,
            recovery_completed,
            ..
        } if rejection_errno == "EINVAL" && *root_still_alive && *recovery_completed => {}
        GuestReadyObservation::RetainedHolderTimeout {
            deadline,
            holder_retained,
            recovery_completed,
            ..
        } if deadline == "EXPIRED" && *holder_retained && *recovery_completed => {}
        GuestReadyObservation::PidReuse {
            original,
            replacement,
            retired_pidfd_signal,
            replacement_alive,
            same_prefix,
            ..
        } if original.pid > 1
            && original.starttime > 0
            && replacement.pid > 1
            && replacement.starttime > 0
            && original != replacement
            && retired_pidfd_signal == "ESRCH"
            && *replacement_alive
            && *same_prefix => {}
        GuestReadyObservation::LateFork {
            late_child,
            observed_after_snapshot,
            post_shutdown,
            cleanup_complete,
            ..
        } if late_child.pid > 1
            && late_child.starttime > 0
            && *observed_after_snapshot
            && post_shutdown == "GONE"
            && *cleanup_complete => {}
        GuestReadyObservation::RootExitBeforeSnapshot {
            root_state,
            snapshot_attempted,
            children_traversed,
            forensic_cleanup_complete,
            ..
        } if root_state == "GONE"
            && *snapshot_attempted
            && !*children_traversed
            && *forensic_cleanup_complete => {}
        _ => return Err(format!("guest observation is invalid for mode {mode}")),
    }
    Ok(json!({
        "status": "GUEST_READY_OBSERVATION_ACCEPTED",
        "trace_id": common.trace_id,
        "mode": mode,
        "terminal": replay.terminal,
        "oracle_status": replay.status,
        "prefix_generation": common.prefix_generation,
        "verifier_semantic_closure_sha256": env!("LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_SHA256"),
        "verifier_workspace_head": env!("LIFECYCLE_FUZZ_WORKSPACE_HEAD"),
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn common(trace_id: &str, mode: &str, terminal: GuestTerminal) -> GuestObservationCommon {
        GuestObservationCommon {
            trace_id: trace_id.to_string(),
            phases: expected_phases(mode).to_vec(),
            terminal,
            unresolved_obligations: Vec::new(),
            source_identity_sha256: "1".repeat(64),
            runtime_identity_sha256: "2".repeat(64),
            verifier_semantic_closure_sha256: env!("LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_SHA256")
                .to_string(),
            prefix_generation: 1,
            prefix: FileIdentity {
                device: 1,
                inode: 2,
                mode: 0o40700,
                uid: 1000,
                gid: 1000,
                nlink: 2,
            },
            session_root: ProcessIdentity {
                pid: 100,
                starttime: 20,
            },
            root_pidfd_retained: true,
            endpoints: vec![
                EndpointObservation {
                    name: ".init.pid".to_string(),
                    identity: FileIdentity {
                        device: 1,
                        inode: 3,
                        mode: libc::S_IFREG | 0o600,
                        uid: 1000,
                        gid: 1000,
                        nlink: 1,
                    },
                },
                EndpointObservation {
                    name: ".darlingserver.sock".to_string(),
                    identity: FileIdentity {
                        device: 1,
                        inode: 4,
                        mode: libc::S_IFSOCK | 0o600,
                        uid: 1000,
                        gid: 1000,
                        nlink: 1,
                    },
                },
                EndpointObservation {
                    name: "var/run/shellspawn.sock".to_string(),
                    identity: FileIdentity {
                        device: 1,
                        inode: 5,
                        mode: libc::S_IFSOCK | 0o600,
                        uid: 1000,
                        gid: 1000,
                        nlink: 1,
                    },
                },
            ],
            route: "OFF".to_string(),
            routing: "DEFERRED".to_string(),
            budgets: GuestBudgets {
                elapsed_ns: 1,
                output_bytes: 1,
                processes_observed: 1,
                max_fds_observed: 1,
            },
        }
    }

    fn trace(trace_id: &str) -> Vec<u8> {
        let hex = canonical_trace_spec(trace_id).expect("trace").0;
        hex.as_bytes()
            .chunks_exact(2)
            .map(|pair| {
                u8::from_str_radix(std::str::from_utf8(pair).expect("hex"), 16).expect("byte")
            })
            .collect()
    }

    #[test]
    fn accepted_gone_observation_uses_reducer_terminal() {
        let observation = GuestReadyObservation::SignalGone {
            common: common(
                "shared-session-signal-gone",
                "signal-gone",
                GuestTerminal::Recovered,
            ),
            post_shutdown_signal: "ESRCH".to_string(),
            cleanup_complete: true,
        };
        assert!(verify_guest_ready_observation(
            &trace("shared-session-signal-gone"),
            &observation,
            &"1".repeat(64),
            &"2".repeat(64),
        )
        .is_ok());
    }

    #[test]
    fn ordering_terminal_identity_and_obligations_fail_closed() {
        let mut observation = GuestReadyObservation::SignalRejected {
            common: common(
                "shared-session-signal-rejected",
                "signal-rejected",
                GuestTerminal::Recovered,
            ),
            rejection_errno: "EINVAL".to_string(),
            root_still_alive: true,
            recovery_completed: true,
        };
        let input = trace("shared-session-signal-rejected");
        let source = "1".repeat(64);
        let runtime = "2".repeat(64);
        assert!(verify_guest_ready_observation(&input, &observation, &source, &runtime).is_ok());
        if let GuestReadyObservation::SignalRejected { common, .. } = &mut observation {
            common.phases.swap(1, 2);
        }
        assert!(verify_guest_ready_observation(&input, &observation, &source, &runtime).is_err());
        if let GuestReadyObservation::SignalRejected { common, .. } = &mut observation {
            common.phases = expected_phases("signal-rejected").to_vec();
            common.terminal = GuestTerminal::Timeout;
        }
        assert!(verify_guest_ready_observation(&input, &observation, &source, &runtime).is_err());
        if let GuestReadyObservation::SignalRejected { common, .. } = &mut observation {
            common.terminal = GuestTerminal::Recovered;
            common
                .unresolved_obligations
                .push("signal-failure".to_string());
        }
        assert!(verify_guest_ready_observation(&input, &observation, &source, &runtime).is_err());
        if let GuestReadyObservation::SignalRejected { common, .. } = &mut observation {
            common.unresolved_obligations.clear();
            common.runtime_identity_sha256 = "forged".to_string();
        }
        assert!(verify_guest_ready_observation(&input, &observation, &source, &runtime).is_err());
    }
}
