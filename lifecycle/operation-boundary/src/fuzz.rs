//! Bounded stateful fuzz target for dar-4ush.4.
//!
//! The bytecode is deliberately small and closed.  It names reducer events,
//! existing operation checkpoints, and deterministic explorer selectors; it
//! never carries paths, descriptors, or an alternate lifecycle model.  The
//! Rust reducer remains the only semantic oracle.  The optional libFuzzer
//! target and the deterministic smoke runner both call [`fuzz_one`].

use crate::explorer::{
    explore, explore_one, ExplorerBudget, FailureBoundary, RecoveryStatus, EXPLORER_SOURCE_COMMIT,
    EXPLORER_SOURCE_TREE, REPRESENTATIVE_INTERLEAVINGS,
};
use crate::state::{
    CapabilityKind, Event, IntentKind, Outcome, RecoveryAction, Reducer, SignalResult, StableState,
    MODEL_MAX_LIVE_CAPABILITIES, MODEL_MAX_RECOVERY_STEPS, MODEL_MAX_VIRTUAL_TIME_NS,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeSet;
use std::fmt;
use std::fs;
use std::path::Path;

pub const BYTECODE_MAGIC: [u8; 4] = *b"DLF1";
pub const BYTECODE_VERSION: u8 = 1;
pub const MAX_INPUT_BYTES: usize = 4096;
pub const MAX_OPS: usize = 64;
pub const MAX_CAPABILITIES: usize = 5;
pub const MAX_SCHEDULE_STEPS: usize = 64;
pub const MAX_OUTPUT_BYTES: usize = 64 * 1024;
pub const MAX_ERROR_RECORDS: usize = 16;
pub const MAX_ERROR_BYTES: usize = 256;
pub const MAX_REPLAY_TRACE: usize = 64;
/// The kernel observation transport is deliberately much smaller than the
/// bytecode input.  Observations are a closed, typed envelope; accepting an
/// arbitrarily large JSON document would make the verifier an unbounded
/// transport endpoint before deserialization can reject it.
pub const MAX_KERNEL_OBSERVATION_BYTES: usize = 16 * 1024;
/// A fuzz campaign may retain only a small bounded number of forensic roots
/// for genuine unsafe outcomes.  Expected CLEAN/FORENSIC_REQUIRED roots are
/// disposed by the target after their manifest has been emitted.
pub const MAX_CAMPAIGN_FORENSIC_ROOTS: usize = 8;

/// Common facts which every real-kernel observation must carry.  Keeping this
/// nested under the mode-tagged enum makes the wire format closed: a mode
/// cannot accidentally smuggle another mode's witness fields into a valid
/// observation.
#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct KernelObservationCommon {
    pub trace_id: String,
    pub event_kinds: Vec<String>,
    pub outcome: String,
    pub filesystem_clean: bool,
    pub pidfd_retained: bool,
}

/// Typed, closed observations returned by the real-kernel `.5.1` transport.
/// The kernel lane does not become a second reducer: Rust validates that the
/// mode-specific witness is admissible for the already accepted bytecode and
/// terminal.  Every variant names all facts needed to prove its claim; there
/// are no optional witness fields or stringly-typed mode cross-products.
#[derive(Debug, Deserialize, Serialize)]
#[serde(tag = "mode", deny_unknown_fields)]
pub enum KernelObservation {
    #[serde(rename = "signal-gone")]
    SignalGone {
        common: KernelObservationCommon,
        signal: String,
        post_exit_signal: String,
    },
    #[serde(rename = "signal-rejected")]
    SignalRejected {
        common: KernelObservationCommon,
        signal: String,
        rejection_errno: String,
    },
    #[serde(rename = "retained-holder-timeout")]
    RetainedHolderTimeout {
        common: KernelObservationCommon,
        deadline: String,
        recovery_completed: bool,
    },
    #[serde(rename = "pid-reuse")]
    PidReuse {
        common: KernelObservationCommon,
        identity: String,
        pid_reuse_classification: String,
        original_pid: u32,
        original_starttime: u64,
        replacement_pid: u32,
        replacement_starttime: u64,
        retired_pidfd_signal: String,
        replacement_alive: bool,
    },
    #[serde(rename = "late-fork")]
    LateFork {
        common: KernelObservationCommon,
        late_fork: String,
        late_pid: u32,
        late_starttime: u64,
        late_fork_observed_by_controller: bool,
        late_fork_acknowledged: bool,
        late_fork_post_drain: String,
    },
    #[serde(rename = "root-exit-before-snapshot")]
    RootExitBeforeSnapshot {
        common: KernelObservationCommon,
        identity: String,
        children_traversed: bool,
    },
}

impl KernelObservation {
    fn common(&self) -> &KernelObservationCommon {
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

/// Feed one host observation back through the Rust oracle.  This intentionally
/// checks only typed kernel facts and the bytecode's terminal; all lifecycle
/// transition semantics remain in `safe_replay`/the reducer.
pub fn verify_kernel_observation(
    input: &[u8],
    observation: &KernelObservation,
) -> Result<Value, String> {
    let common = observation.common();
    let mode = observation.mode();
    let (canonical_hex, expected_mode, expected_events) = canonical_trace_spec(&common.trace_id)
        .ok_or_else(|| format!("unknown canonical kernel trace {}", common.trace_id))?;
    if hex_encode(input) != canonical_hex {
        return Err("bytecode does not match canonical trace identity".to_string());
    }
    if mode != expected_mode {
        return Err("kernel mode does not match canonical trace identity".to_string());
    }
    if common
        .event_kinds
        .iter()
        .map(String::as_str)
        .ne(expected_events.iter().copied())
    {
        return Err("kernel event sequence does not match canonical trace".to_string());
    }
    let report = safe_replay(input);
    if report.status != "ACCEPTED" {
        return Err(format!(
            "kernel observation attached to {} trace",
            report.status
        ));
    }
    if report.terminal.as_deref() != Some(common.outcome.as_str()) {
        return Err("kernel observation terminal differs from Rust oracle".to_string());
    }
    if common.trace_id.is_empty()
        || common.event_kinds.is_empty()
        || !common.filesystem_clean
        || !common.pidfd_retained
    {
        return Err("kernel observation omitted required bounded safety facts".to_string());
    }
    match observation {
        KernelObservation::SignalGone {
            signal,
            post_exit_signal,
            ..
        } if signal == "GONE" && post_exit_signal == "ESRCH" => {}
        KernelObservation::SignalRejected {
            signal,
            rejection_errno,
            ..
        } if signal == "REJECTED" && rejection_errno == "EINVAL" => {}
        KernelObservation::RetainedHolderTimeout {
            deadline,
            recovery_completed,
            ..
        } if deadline == "EXPIRED" && common.outcome == "TIMEOUT" && *recovery_completed => {}
        KernelObservation::PidReuse {
            identity,
            pid_reuse_classification,
            original_pid,
            original_starttime,
            replacement_pid,
            replacement_starttime,
            retired_pidfd_signal,
            replacement_alive,
            ..
        } if identity == "MISMATCH"
            && *original_pid > 0
            && *replacement_pid > 0
            && *original_starttime > 0
            && *replacement_starttime > 0
            && retired_pidfd_signal == "ESRCH"
            && *replacement_alive
            && match pid_reuse_classification.as_str() {
                "NUMERIC_PID_REUSE_STARTTIME_MISMATCH" => {
                    original_pid == replacement_pid && original_starttime != replacement_starttime
                }
                "RETIRED_PIDFD_REPLACEMENT_NOT_TARGETED" => original_pid != replacement_pid,
                _ => false,
            } => {}
        KernelObservation::LateFork {
            late_fork,
            late_pid,
            late_starttime,
            late_fork_observed_by_controller,
            late_fork_acknowledged,
            late_fork_post_drain,
            ..
        } if late_fork == "OBSERVED_AND_REAPED"
            && *late_pid > 0
            && *late_starttime > 0
            && *late_fork_observed_by_controller
            && *late_fork_acknowledged
            && late_fork_post_drain == "GONE" => {}
        KernelObservation::RootExitBeforeSnapshot {
            identity,
            children_traversed,
            ..
        } if identity == "GONE" && !*children_traversed => {}
        _ => return Err(format!("kernel observation is invalid for mode {mode}")),
    }
    Ok(json!({
        "status": "KERNEL_OBSERVATION_ACCEPTED",
        "trace_id": common.trace_id,
        "mode": mode,
        "terminal": report.terminal,
        "oracle_status": report.status,
    }))
}

fn canonical_trace_spec(
    trace_id: &str,
) -> Option<(&'static str, &'static str, &'static [&'static str])> {
    Some(match trace_id {
        "shared-session-signal-gone" => (
            "444c463101003aac30275d690e65000002060003010200030001050000010b03",
            "signal-gone",
            &[
                "intent_declared",
                "barrier_entered",
                "capability_acquired",
                "identity_revalidated",
                "signal_sent",
                "terminal",
            ],
        ),
        "shared-session-signal-rejected" => (
            "444c463101007de40dd4201de3c70000020700030102000300010500000206090b03",
            "signal-rejected",
            &[
                "intent_declared",
                "barrier_entered",
                "capability_acquired",
                "identity_revalidated",
                "signal_sent",
                "recovery",
                "terminal",
            ],
        ),
        "shared-session-retained-holder-timeout" => (
            "444c463101004716e91850bccfa50000020f00030102000300010201020304030003010103030105000000070007010a0506090b02",
            "retained-holder-timeout",
            &[
                "intent_declared",
                "barrier_entered",
                "capability_acquired",
                "capability_acquired",
                "capability_acquired",
                "membership_snapshot",
                "identity_revalidated",
                "identity_revalidated",
                "identity_revalidated",
                "signal_sent",
                "capability_released",
                "capability_released",
                "fault_injected",
                "recovery",
                "terminal",
            ],
        ),
        "shared-session-pid-reuse" => (
            "444c463101009a4e9eede612eed300000206000301020003000006040b01",
            "pid-reuse",
            &[
                "intent_declared",
                "barrier_entered",
                "capability_acquired",
                "identity_revalidated",
                "recovery",
                "terminal",
            ],
        ),
        "shared-session-late-fork" => (
            "444c46310100fff15cebd6d822ae0000020a000301020003000102010403010902020a0406040b01",
            "late-fork",
            &[
                "intent_declared",
                "barrier_entered",
                "capability_acquired",
                "capability_acquired",
                "membership_snapshot",
                "member_observed",
                "fault_injected",
                "recovery",
                "terminal",
            ],
        ),
        "session-root-exit-before-snapshot" => (
            "444c4631010094658829503ae61d00000206000301020003000006040b01",
            "root-exit-before-snapshot",
            &[
                "intent_declared",
                "barrier_entered",
                "capability_acquired",
                "identity_revalidated",
                "recovery",
                "terminal",
            ],
        ),
        _ => return None,
    })
}

/// The reducer's base registry is shared with the v1 lifecycle model.  Fuzz
/// historical arms additionally require these defect-specific predicates;
/// keeping that extension here prevents the transport/reference model from
/// silently changing while making the fuzz oracle's RED arms executable.
pub const FUZZ_INVARIANT_REGISTRY: &[&str] = &[
    // The accepted lifecycle invariants from the shared reducer model.
    "no-raw-path-authority",
    "no-unvalidated-fd",
    "stable-journal-independent",
    "total-recovery-matrix",
    "identity-before-signal",
    "no-children-after-gone",
    "shared-lease-bound",
    "pidfd-identity-before-signal",
    "late-fork-closes-snapshot",
    "bounded-replay",
    "terminal-trace",
    // Defect-specific predicates used only by typed historical RED arms.
    "identity-failure-recovered",
    "signal-failure-recovered",
    "deadline-recovered",
    "operation-failure-recovered",
    "quarantine-recovery",
    "inode-aba-revalidated",
    "pid-reuse-revalidated",
    "root-gone-membership-revalidated",
];

const CAPABILITY_IDS: [&str; MAX_CAPABILITIES] = [
    "cap.session-root",
    "cap.launchd-member",
    "cap.late-member",
    "cap.shared-lease",
    "cap.launchd-endpoint",
];

const CHECKPOINT_NAMES: [&str; 9] = [
    "after-mkdir-before-bind",
    "after-bind-before-publish",
    "after-stage-mkdir-before-bind",
    "after-stage-bind-before-move",
    "after-move-before-verify",
    "after-quarantine-move-before-verify",
    "after-quarantine-verify-before-gc",
    "before-exact-mutation",
    "after-exact-mutation",
];

const OPERATION_NAMES: [&str; 3] = ["MKDIR_CHILD", "UNLINK_EXACT", "RENAME_EXACT"];
const FAULT_NAMES: [&str; 7] = [
    "SIGINT",
    "TIMEOUT",
    "ROOT_EXIT",
    "PID_REUSE",
    "LATE_FORK",
    "LEASE_HOLDER",
    "CLOSE_RANGE",
];
const ORIGIN_NAMES: [&str; 4] = ["STARTUP_LEDGER", "SNAPSHOT", "LATE_FORK", "REPLACEMENT"];
const COMPLETENESS_NAMES: [&str; 5] = ["COMPLETE", "CLOSED", "ABORTED_GONE", "OVERFLOW", "TIMEOUT"];

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum FuzzMode {
    Reducer = 0,
    Explorer = 1,
}

impl FuzzMode {
    fn decode(value: u8) -> Result<Self, DecodeError> {
        match value {
            0 => Ok(Self::Reducer),
            1 => Ok(Self::Explorer),
            _ => Err(DecodeError::new("unknown fuzz mode")),
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Reducer => "reducer",
            Self::Explorer => "explorer",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum BytecodeOp {
    Intent(u8),
    Barrier,
    Acquire(u8),
    Identity {
        capability: u8,
        matches: bool,
    },
    Membership {
        mask: u8,
        completeness: u8,
    },
    Signal {
        capability: u8,
        signal: u8,
        result: u8,
    },
    Recovery(u8),
    Release(u8),
    Checkpoint {
        operation: u8,
        checkpoint: u8,
        placement: u8,
        result: u8,
    },
    MemberObserved {
        capability: u8,
        origin: u8,
    },
    Fault(u8),
    Terminal(u8),
    Advance(u16),
    Noop,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScenarioBytecode {
    pub mode: FuzzMode,
    pub seed: u64,
    pub boundary: u8,
    pub interleaving: u8,
    pub initial_stable: u8,
    pub ops: Vec<BytecodeOp>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DecodeError {
    message: String,
}

impl DecodeError {
    fn new(message: &str) -> Self {
        Self {
            message: message.to_string(),
        }
    }
}

impl fmt::Display for DecodeError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for DecodeError {}

impl ScenarioBytecode {
    pub fn decode(input: &[u8]) -> Result<Self, DecodeError> {
        if input.len() > MAX_INPUT_BYTES {
            return Err(DecodeError::new("bytecode exceeds input limit"));
        }
        let mut reader = Reader { input, offset: 0 };
        if reader.take(4)? != BYTECODE_MAGIC {
            return Err(DecodeError::new("invalid bytecode magic"));
        }
        if reader.u8()? != BYTECODE_VERSION {
            return Err(DecodeError::new("unsupported bytecode version"));
        }
        let mode = FuzzMode::decode(reader.u8()?)?;
        let seed = reader.u64()?;
        let boundary = reader.u8()?;
        let interleaving = reader.u8()?;
        let initial_stable = reader.u8()?;
        if initial_stable > 5 {
            return Err(DecodeError::new("invalid initial stable state"));
        }
        if boundary >= 16 {
            return Err(DecodeError::new("invalid failure boundary"));
        }
        if interleaving >= 12 {
            return Err(DecodeError::new("invalid interleaving"));
        }
        let count = reader.u8()? as usize;
        if count > MAX_OPS {
            return Err(DecodeError::new("operation count exceeds limit"));
        }
        let mut ops = Vec::with_capacity(count);
        for _ in 0..count {
            ops.push(reader.op()?)
        }
        if reader.remaining() != 0 {
            return Err(DecodeError::new("trailing bytecode data"));
        }
        Ok(Self {
            mode,
            seed,
            boundary,
            interleaving,
            initial_stable,
            ops,
        })
    }

    pub fn encode(&self) -> Result<Vec<u8>, DecodeError> {
        if self.ops.len() > MAX_OPS {
            return Err(DecodeError::new("operation count exceeds limit"));
        }
        if self.boundary >= 16 || self.interleaving >= 12 || self.initial_stable > 5 {
            return Err(DecodeError::new("bytecode selector is out of range"));
        }
        let mut output = Vec::with_capacity(32 + self.ops.len() * 5);
        output.extend_from_slice(&BYTECODE_MAGIC);
        output.push(BYTECODE_VERSION);
        output.push(self.mode as u8);
        output.extend_from_slice(&self.seed.to_le_bytes());
        output.push(self.boundary);
        output.push(self.interleaving);
        output.push(self.initial_stable);
        output.push(self.ops.len() as u8);
        for op in &self.ops {
            encode_op(&mut output, *op)?;
        }
        if output.len() > MAX_INPUT_BYTES {
            return Err(DecodeError::new("encoded bytecode exceeds input limit"));
        }
        Ok(output)
    }

    pub fn hex(&self) -> Result<String, DecodeError> {
        Ok(hex_encode(&self.encode()?))
    }
}

struct Reader<'a> {
    input: &'a [u8],
    offset: usize,
}

impl<'a> Reader<'a> {
    fn take(&mut self, length: usize) -> Result<&'a [u8], DecodeError> {
        let end = self
            .offset
            .checked_add(length)
            .ok_or_else(|| DecodeError::new("bytecode offset overflow"))?;
        if end > self.input.len() {
            return Err(DecodeError::new("truncated bytecode"));
        }
        let value = &self.input[self.offset..end];
        self.offset = end;
        Ok(value)
    }

    fn u8(&mut self) -> Result<u8, DecodeError> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> Result<u16, DecodeError> {
        let bytes = self.take(2)?;
        Ok(u16::from_le_bytes([bytes[0], bytes[1]]))
    }

    fn u64(&mut self) -> Result<u64, DecodeError> {
        let bytes = self.take(8)?;
        Ok(u64::from_le_bytes([
            bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
        ]))
    }

    fn remaining(&self) -> usize {
        self.input.len().saturating_sub(self.offset)
    }

    fn op(&mut self) -> Result<BytecodeOp, DecodeError> {
        let tag = self.u8()?;
        match tag {
            0 => Ok(BytecodeOp::Intent(self.enum_value(6, "intent")?)),
            1 => Ok(BytecodeOp::Barrier),
            2 => Ok(BytecodeOp::Acquire(self.capability()?)),
            3 => Ok(BytecodeOp::Identity {
                capability: self.capability()?,
                matches: self.bool_value("identity result")?,
            }),
            4 => Ok(BytecodeOp::Membership {
                mask: self.u8()?,
                completeness: self.enum_value(4, "membership completeness")?,
            }),
            5 => Ok(BytecodeOp::Signal {
                capability: self.capability()?,
                signal: self.enum_value(3, "signal")?,
                result: self.enum_value(3, "signal result")?,
            }),
            6 => Ok(BytecodeOp::Recovery(
                self.enum_value(11, "recovery action")?,
            )),
            7 => Ok(BytecodeOp::Release(self.capability()?)),
            8 => Ok(BytecodeOp::Checkpoint {
                operation: self.enum_value(2, "operation")?,
                checkpoint: self.enum_value(8, "checkpoint")?,
                placement: self.enum_value(1, "checkpoint placement")?,
                result: self.enum_value(2, "checkpoint result")?,
            }),
            9 => Ok(BytecodeOp::MemberObserved {
                capability: self.capability()?,
                origin: self.enum_value(3, "member origin")?,
            }),
            10 => Ok(BytecodeOp::Fault(self.enum_value(6, "fault")?)),
            11 => Ok(BytecodeOp::Terminal(
                self.enum_value(3, "terminal outcome")?,
            )),
            12 => Ok(BytecodeOp::Advance(self.u16()?)),
            13 => Ok(BytecodeOp::Noop),
            _ => Err(DecodeError::new("unknown bytecode operation")),
        }
    }

    fn capability(&mut self) -> Result<u8, DecodeError> {
        let value = self.u8()?;
        if value as usize >= MAX_CAPABILITIES {
            return Err(DecodeError::new("capability index out of range"));
        }
        Ok(value)
    }

    fn bool_value(&mut self, name: &str) -> Result<bool, DecodeError> {
        match self.u8()? {
            0 => Ok(false),
            1 => Ok(true),
            _ => Err(DecodeError::new(name)),
        }
    }

    fn enum_value(&mut self, max: u8, name: &str) -> Result<u8, DecodeError> {
        let value = self.u8()?;
        if value > max {
            return Err(DecodeError::new(name));
        }
        Ok(value)
    }
}

fn encode_op(output: &mut Vec<u8>, op: BytecodeOp) -> Result<(), DecodeError> {
    match op {
        BytecodeOp::Intent(value) => {
            check_value(value, 6, "intent")?;
            output.extend_from_slice(&[0, value]);
        }
        BytecodeOp::Barrier => output.push(1),
        BytecodeOp::Acquire(capability) => {
            check_capability(capability)?;
            output.extend_from_slice(&[2, capability]);
        }
        BytecodeOp::Identity {
            capability,
            matches,
        } => {
            check_capability(capability)?;
            output.extend_from_slice(&[3, capability, matches as u8]);
        }
        BytecodeOp::Membership { mask, completeness } => {
            check_value(completeness, 4, "membership completeness")?;
            output.extend_from_slice(&[4, mask, completeness]);
        }
        BytecodeOp::Signal {
            capability,
            signal,
            result,
        } => {
            check_capability(capability)?;
            check_value(signal, 3, "signal")?;
            check_value(result, 3, "signal result")?;
            output.extend_from_slice(&[5, capability, signal, result]);
        }
        BytecodeOp::Recovery(value) => {
            check_value(value, 11, "recovery action")?;
            output.extend_from_slice(&[6, value]);
        }
        BytecodeOp::Release(capability) => {
            check_capability(capability)?;
            output.extend_from_slice(&[7, capability]);
        }
        BytecodeOp::Checkpoint {
            operation,
            checkpoint,
            placement,
            result,
        } => {
            check_value(operation, 2, "operation")?;
            check_value(checkpoint, 8, "checkpoint")?;
            check_value(placement, 1, "checkpoint placement")?;
            check_value(result, 2, "checkpoint result")?;
            output.extend_from_slice(&[8, operation, checkpoint, placement, result]);
        }
        BytecodeOp::MemberObserved { capability, origin } => {
            check_capability(capability)?;
            check_value(origin, 3, "member origin")?;
            output.extend_from_slice(&[9, capability, origin]);
        }
        BytecodeOp::Fault(value) => {
            check_value(value, 6, "fault")?;
            output.extend_from_slice(&[10, value]);
        }
        BytecodeOp::Terminal(value) => {
            check_value(value, 3, "terminal outcome")?;
            output.extend_from_slice(&[11, value]);
        }
        BytecodeOp::Advance(value) => {
            output.extend_from_slice(&[12, (value & 0xff) as u8, (value >> 8) as u8])
        }
        BytecodeOp::Noop => output.push(13),
    }
    Ok(())
}

fn check_value(value: u8, max: u8, name: &str) -> Result<(), DecodeError> {
    if value > max {
        Err(DecodeError::new(name))
    } else {
        Ok(())
    }
}

fn check_capability(value: u8) -> Result<(), DecodeError> {
    if value as usize >= MAX_CAPABILITIES {
        Err(DecodeError::new("capability index out of range"))
    } else {
        Ok(())
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ReplayReport {
    pub status: String,
    pub mode: String,
    pub seed: u64,
    pub event_count: usize,
    pub virtual_time_ns: u64,
    pub recovery_steps: usize,
    pub schedule_steps: usize,
    pub terminal: Option<String>,
    pub unresolved_obligations: Vec<String>,
    pub errors: Vec<String>,
    /// Number of reducer events rejected by the typed oracle.  A rejected
    /// input is a normal fuzzer result, not an invariant crash; keeping this
    /// count in the report lets the target distinguish malformed transitions
    /// from an accepted trace that violates a safety invariant.
    pub rejected_events: usize,
    pub coverage_edges: Vec<u16>,
    pub coverage_hash: String,
    pub minimized_bytecode_hex: Option<String>,
    pub minimized_replay_trace: Vec<String>,
    pub scenario: Option<String>,
    pub recovery_status: Option<String>,
    pub forensic_roots: Vec<String>,
    pub source_identity: Value,
    pub resource_census: Value,
    pub reproduction_command: String,
    pub failure_fingerprint: FailureFingerprint,
}

/// Stable semantic identity for a failure.  Minimization is allowed to remove
/// irrelevant events, but it may never replace the missing invariant,
/// normalized error class, scenario, or recovery classification.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct FailureFingerprint {
    pub status: String,
    pub missing_invariants: Vec<String>,
    pub error_classes: Vec<String>,
    pub scenario: Option<String>,
    pub recovery_status: Option<String>,
}

impl FailureFingerprint {
    fn empty(status: &str) -> Self {
        Self {
            status: status.to_string(),
            missing_invariants: Vec::new(),
            error_classes: Vec::new(),
            scenario: None,
            recovery_status: None,
        }
    }
}

fn error_class(error: &str) -> String {
    if error.starts_with("invariant:") {
        "invariant".to_string()
    } else if error.starts_with("event ") {
        "event-rejection".to_string()
    } else if error.contains("budget exceeded") {
        "budget".to_string()
    } else {
        error
            .split(':')
            .next()
            .unwrap_or(error)
            .split_whitespace()
            .next()
            .unwrap_or(error)
            .to_string()
    }
}

fn missing_invariants(errors: &[String]) -> Vec<String> {
    let mut values = BTreeSet::new();
    for error in errors {
        if let Some(list) = error.strip_prefix("invariant: missing=") {
            values.extend(
                list.split(',')
                    .filter(|value| !value.is_empty())
                    .map(str::to_string),
            );
        }
    }
    values.into_iter().collect()
}

fn failure_fingerprint(
    status: &str,
    errors: &[String],
    scenario: Option<&str>,
    recovery_status: Option<&str>,
) -> FailureFingerprint {
    let mut classes = BTreeSet::new();
    classes.extend(errors.iter().map(|error| error_class(error)));
    FailureFingerprint {
        status: status.to_string(),
        missing_invariants: missing_invariants(errors),
        error_classes: classes.into_iter().collect(),
        scenario: scenario.map(str::to_string),
        recovery_status: recovery_status.map(str::to_string),
    }
}

pub fn fuzz_one(input: &[u8]) -> Result<ReplayReport, DecodeError> {
    let program = ScenarioBytecode::decode(input)?;
    match program.mode {
        // LibFuzzer itself supplies the exact crashing input.  Avoid doing
        // quadratic delta minimization on every mutation; the deterministic
        // replay CLI performs minimization when it emits a failure report.
        FuzzMode::Reducer => {
            let report = replay_reducer(&program, false)?;
            if report.status == "INVARIANT_VIOLATION" {
                replay_reducer(&program, true)
            } else {
                Ok(report)
            }
        }
        // A libFuzzer input has a hard per-input filesystem bound.  It still
        // executes the selected boundary/interleaving pair; selectors are not
        // normalized to a canonical no-op scenario.  The complete matrix is
        // available through replay_program()/the deterministic contract.
        FuzzMode::Explorer => replay_fuzz_explorer(&program),
    }
}

fn replay_fuzz_explorer(program: &ScenarioBytecode) -> Result<ReplayReport, DecodeError> {
    replay_explorer_with_budget(
        program,
        ExplorerBudget {
            max_scenarios: 1,
            ..ExplorerBudget::default()
        },
    )
}

pub fn replay_program(program: &ScenarioBytecode) -> Result<ReplayReport, DecodeError> {
    match program.mode {
        FuzzMode::Reducer => replay_reducer(program, true),
        FuzzMode::Explorer => replay_explorer_with_budget(program, ExplorerBudget::default()),
    }
}

fn replay_reducer(
    program: &ScenarioBytecode,
    minimize_failures: bool,
) -> Result<ReplayReport, DecodeError> {
    let initial = stable_state(program.initial_stable)?;
    let mut reducer = Reducer::new(initial);
    register_catalog(&mut reducer).map_err(|error| DecodeError::new(&error.to_string()))?;
    let mut errors = Vec::new();
    let mut coverage = BTreeSet::new();
    let mut virtual_time_ns = 0u64;
    let mut recovery_steps = 0usize;
    let mut schedule_steps = 0usize;
    let mut rejected_events = 0usize;
    for (index, op) in program.ops.iter().copied().enumerate() {
        // Terminal is a one-shot boundary.  Even operations that do not
        // produce reducer events (Noop/Advance) are part of the bytecode
        // trace and therefore cannot appear after it.
        if reducer.terminal().is_some() {
            rejected_events += 1;
            coverage.insert(0x4000 + op_tag(op) as u16);
            if errors.len() < MAX_ERROR_RECORDS {
                errors.push(truncate_error(&format!(
                    "event {index}: event after terminal"
                )));
            }
            continue;
        }
        if schedule_steps >= MAX_SCHEDULE_STEPS {
            errors.push("schedule budget exceeded".to_string());
            coverage.insert(0x7ff0);
            break;
        }
        schedule_steps += 1;
        let delta = match op {
            BytecodeOp::Advance(value) => value as u64,
            _ => 100,
        };
        virtual_time_ns = match virtual_time_ns.checked_add(delta) {
            Some(value) if value <= MODEL_MAX_VIRTUAL_TIME_NS => value,
            _ => {
                errors.push("virtual time budget exceeded".to_string());
                coverage.insert(0x7ff1);
                break;
            }
        };
        let tag = op_tag(op);
        coverage.insert(0x1000 + tag as u16);
        let event = match event_for_op(op) {
            Ok(Some(event)) => event,
            Ok(None) => continue,
            Err(error) => {
                rejected_events += 1;
                errors.push(error.message);
                coverage.insert(0x4000 + tag as u16);
                continue;
            }
        };
        if let Event::Recovery(_) = event {
            recovery_steps += 1;
            if recovery_steps > MODEL_MAX_RECOVERY_STEPS {
                errors.push("recovery budget exceeded".to_string());
                coverage.insert(0x7ff2);
                break;
            }
        }
        if let Err(error) = reducer.apply(event) {
            rejected_events += 1;
            coverage.insert(0x4000 + tag as u16);
            if errors.len() < MAX_ERROR_RECORDS {
                errors.push(truncate_error(&format!("event {index}: {error}")));
            }
        } else {
            coverage.insert(0x2000 + snapshot_code(reducer.snapshot()));
            if reducer.live().len() > MODEL_MAX_LIVE_CAPABILITIES {
                errors.push("live capability budget exceeded".to_string());
                coverage.insert(0x7ff3);
                break;
            }
        }
    }
    let terminal = reducer.terminal().map(outcome_name).map(str::to_string);
    let base_status = if errors.iter().any(|error| error.contains("budget exceeded")) {
        "BUDGET_EXCEEDED"
    } else if !errors.is_empty() {
        "REJECTED"
    } else if terminal.is_none() {
        "INCOMPLETE"
    } else {
        "ACCEPTED"
    };
    // The reducer is the fuzz oracle, not merely an event interpreter.  A
    // trace that reaches a terminal but drops a registered safety invariant
    // is a semantic failure even when every individual event was accepted.
    // In particular, shared-session leases may never be left live at SUCCESS.
    let satisfied = reducer.invariant_names(
        program.ops.len(),
        virtual_time_ns,
        MAX_OPS,
        MODEL_MAX_VIRTUAL_TIME_NS,
        MAX_CAPABILITIES,
        MODEL_MAX_RECOVERY_STEPS,
    );
    // These eight predicates are deliberately scoped to the fuzz oracle's
    // historical RED arms.  They are not part of the accepted v1 lifecycle
    // protocol exposed by the Rust boundary/Python differential model.
    let mut satisfied = satisfied;
    satisfied.extend(
        [
            "identity-failure-recovered",
            "signal-failure-recovered",
            "deadline-recovered",
            "operation-failure-recovered",
            "quarantine-recovery",
            "inode-aba-revalidated",
            "pid-reuse-revalidated",
            "root-gone-membership-revalidated",
        ]
        .into_iter()
        .map(str::to_string),
    );
    for (obligation, invariant) in [
        ("identity-failure", "identity-failure-recovered"),
        ("signal-failure", "signal-failure-recovered"),
        ("fault:TIMEOUT", "deadline-recovered"),
        ("operation-failure", "operation-failure-recovered"),
        ("quarantine-failure", "quarantine-recovery"),
        ("inode-aba-failure", "inode-aba-revalidated"),
        ("fault:PID_REUSE", "pid-reuse-revalidated"),
        ("fault:ROOT_EXIT", "root-gone-membership-revalidated"),
    ] {
        if reducer.obligations().contains(obligation) {
            satisfied.remove(invariant);
        }
    }
    let missing_invariants: Vec<&str> = FUZZ_INVARIANT_REGISTRY
        .iter()
        .copied()
        .filter(|invariant| !satisfied.contains(*invariant))
        .collect();
    if !missing_invariants.is_empty()
        && rejected_events == 0
        && !errors.iter().any(|error| error.starts_with("invariant:"))
    {
        errors.push(truncate_error(&format!(
            "invariant: missing={}",
            missing_invariants.join(",")
        )));
    }
    let status = if base_status == "ACCEPTED"
        && !missing_invariants.is_empty()
        && terminal.is_some()
        && rejected_events == 0
    {
        "INVARIANT_VIOLATION"
    } else {
        base_status
    };
    let status_name = status.to_string();
    let expected_fingerprint = failure_fingerprint(&status_name, &errors, None, None);
    let minimized_program = if minimize_failures && !errors.is_empty() {
        minimize_reducer_program(program, &expected_fingerprint)
    } else {
        program.clone()
    };
    let encoded = minimized_program.hex().ok();
    let minimized_replay_trace = minimized_program
        .ops
        .iter()
        .take(MAX_REPLAY_TRACE)
        .map(|op| op_name(*op).to_string())
        .collect();
    let output = ReplayReport {
        status: status_name,
        mode: program.mode.as_str().to_string(),
        seed: program.seed,
        event_count: program.ops.len(),
        virtual_time_ns,
        recovery_steps,
        schedule_steps,
        terminal,
        unresolved_obligations: reducer.unresolved_obligations(),
        errors,
        rejected_events,
        coverage_edges: coverage.iter().copied().collect(),
        coverage_hash: coverage_hash(&coverage),
        minimized_bytecode_hex: encoded.clone(),
        minimized_replay_trace,
        scenario: None,
        recovery_status: None,
        forensic_roots: Vec::new(),
        source_identity: source_identity(),
        resource_census: json!({
            "input_bytes": program.encode().map(|value| value.len()).unwrap_or(MAX_INPUT_BYTES),
            "events": program.ops.len(),
            "capability_limit": MAX_CAPABILITIES,
            "virtual_time_limit_ns": MODEL_MAX_VIRTUAL_TIME_NS,
            "schedule_limit": MAX_SCHEDULE_STEPS,
            "recovery_limit": MODEL_MAX_RECOVERY_STEPS,
            "filesystem_roots_created": 0,
        }),
        reproduction_command: format!(
            "lifecycle-fuzz --replay-hex {}",
            encoded.unwrap_or_default()
        ),
        failure_fingerprint: expected_fingerprint,
    };
    ensure_output_bound(&output)?;
    Ok(output)
}

fn minimize_reducer_program(
    program: &ScenarioBytecode,
    expected_fingerprint: &FailureFingerprint,
) -> ScenarioBytecode {
    let mut candidate = program.clone();
    let mut index = 0usize;
    while index < candidate.ops.len() {
        let mut trial = candidate.clone();
        trial.ops.remove(index);
        let keeps_failure = replay_reducer(&trial, false)
            .map(|report| report.failure_fingerprint == *expected_fingerprint)
            .unwrap_or(false);
        if keeps_failure {
            candidate = trial;
        } else {
            index += 1;
        }
    }
    candidate
}

fn replay_explorer_with_budget(
    program: &ScenarioBytecode,
    budget: ExplorerBudget,
) -> Result<ReplayReport, DecodeError> {
    let boundary = FailureBoundary::from_index(program.boundary)
        .ok_or_else(|| DecodeError::new("explorer boundary selector out of range"))?;
    let interleaving = REPRESENTATIVE_INTERLEAVINGS
        .get(program.interleaving as usize)
        .copied()
        .ok_or_else(|| DecodeError::new("explorer interleaving selector out of range"))?;
    let scenario = explore_one(
        program.seed ^ operation_schedule_digest(&program.ops),
        boundary,
        interleaving,
        budget,
    )
    .map_err(|error| DecodeError::new(&error.to_string()))?;
    if let Some(path) = scenario.forensic_root.as_deref() {
        // Every retained root, including a genuine UNSAFE/UNDETECTED crash
        // root, carries the bounded typed manifest before the target decides
        // whether to keep or dispose it.
        write_forensic_manifest(&scenario, path)?;
    }
    let boundary_name = boundary.as_str();
    let interleaving_name = interleaving.as_str();
    if scenario.failure_boundary != boundary_name || scenario.interleaving != interleaving_name {
        return Err(DecodeError::new(
            "explorer selector is not in the declared matrix",
        ));
    }
    let mut coverage = BTreeSet::new();
    coverage.insert(0x6000 + program.boundary as u16);
    coverage.insert(0x6100 + program.interleaving as u16);
    coverage.insert(0x6200 + recovery_code(scenario.recovery_status));
    let recovery_status_name = recovery_status_name(scenario.recovery_status);
    let scenario_fingerprint = failure_fingerprint(
        "EXPLORER_OBSERVED",
        &scenario.unresolved_obligations,
        Some(&scenario.scenario),
        Some(recovery_status_name),
    );
    let output = ReplayReport {
        status: "EXPLORER_OBSERVED".to_string(),
        mode: program.mode.as_str().to_string(),
        seed: program.seed,
        event_count: scenario.minimized_event_count,
        virtual_time_ns: scenario.virtual_time_ns,
        recovery_steps: scenario.schedule_steps,
        schedule_steps: scenario.schedule_steps,
        terminal: Some(scenario.outcome.clone()),
        unresolved_obligations: scenario.unresolved_obligations.clone(),
        errors: Vec::new(),
        rejected_events: 0,
        coverage_edges: coverage.iter().copied().collect(),
        coverage_hash: coverage_hash(&coverage),
        minimized_bytecode_hex: program.hex().ok(),
        minimized_replay_trace: vec![scenario.scenario.clone()],
        scenario: Some(scenario.scenario.clone()),
        recovery_status: Some(recovery_status_name.to_string()),
        forensic_roots: scenario
            .forensic_root
            .as_ref()
            .map(|path| vec![path.clone()])
            .unwrap_or_default(),
        source_identity: source_identity(),
        resource_census: json!({
            "input_bytes": program.encode().map(|value| value.len()).unwrap_or(MAX_INPUT_BYTES),
            "events": scenario.minimized_event_count,
            "capability_limit": MAX_CAPABILITIES,
            "virtual_time_limit_ns": MODEL_MAX_VIRTUAL_TIME_NS,
            "schedule_limit": MAX_SCHEDULE_STEPS,
            "recovery_limit": MODEL_MAX_RECOVERY_STEPS,
            "filesystem_roots_created": usize::from(scenario.forensic_root.is_some()),
        }),
        reproduction_command: format!(
            "TMPDIR=$OWNED_TMPDIR lifecycle-fuzz --replay-hex {}",
            program.hex().unwrap_or_default()
        ),
        failure_fingerprint: scenario_fingerprint,
    };
    ensure_output_bound(&output)?;
    Ok(output)
}

fn remove_owned_forensic_root(path: &str) -> Result<(), DecodeError> {
    let tmpdir = std::env::var_os("TMPDIR")
        .as_deref()
        .map(Path::new)
        .ok_or_else(|| DecodeError::new("TMPDIR is required for explorer replay"))?
        .canonicalize()
        .map_err(|_| DecodeError::new("TMPDIR is not accessible"))?;
    let root = Path::new(path)
        .canonicalize()
        .map_err(|_| DecodeError::new("forensic root identity"))?;
    if !root.starts_with(&tmpdir) || root == tmpdir {
        return Err(DecodeError::new("forensic root escaped TMPDIR"));
    }
    fs::remove_dir_all(root).map_err(|_| DecodeError::new("forensic root cleanup"))?;
    Ok(())
}

/// Apply the ownership policy for explorer roots returned by one fuzz input.
/// The explorer itself must preserve FORENSIC_REQUIRED evidence until its
/// caller has extracted the typed manifest.  The libFuzzer target is that
/// owner: expected outcomes are removed immediately, while a genuine
/// UNSAFE/UNDETECTED (or accepted reducer invariant failure) retains its root
/// for the crash artifact.  The per-input bound prevents a malformed report
/// from turning one input into an unbounded filesystem allocation.
pub fn dispose_replay_roots(
    report: &ReplayReport,
    retain_forensic: bool,
) -> Result<(), DecodeError> {
    if report.forensic_roots.len() > 1 {
        return Err(DecodeError::new("per-input forensic root budget exceeded"));
    }
    if report
        .resource_census
        .get("filesystem_roots_created")
        .and_then(Value::as_u64)
        .is_some_and(|value| value > 1)
    {
        return Err(DecodeError::new("filesystem root census exceeded"));
    }
    if retain_forensic {
        return Ok(());
    }
    for path in &report.forensic_roots {
        remove_owned_forensic_root(path)?;
    }
    Ok(())
}

fn recovery_status_name(status: RecoveryStatus) -> &'static str {
    match status {
        RecoveryStatus::Clean => "CLEAN",
        RecoveryStatus::ForensicRequired => "FORENSIC_REQUIRED",
        RecoveryStatus::Unsafe => "UNSAFE",
        RecoveryStatus::Undetected => "UNDETECTED",
    }
}

fn write_forensic_manifest(
    scenario: &crate::explorer::ScenarioResult,
    root: &str,
) -> Result<(), DecodeError> {
    let root_path = Path::new(root);
    if !root_path.is_dir() {
        return Err(DecodeError::new("forensic root is not a directory"));
    }
    let manifest = json!({
        "schema": "darling.lifecycle-fuzz.forensic-manifest.v1",
        "scenario": scenario.scenario,
        "seed": scenario.seed,
        "failure_boundary": scenario.failure_boundary,
        "interleaving": scenario.interleaving,
        "recovery_status": recovery_status_name(scenario.recovery_status),
        "fault_detected": scenario.fault_detected,
        "safety_preserved": scenario.safety_preserved,
        "filesystem_postcondition": scenario.filesystem_postcondition,
        "stage_obligation_ids": scenario.stage_obligation_ids,
        "quarantine_obligation_ids": scenario.quarantine_obligation_ids,
        "source_identity": source_identity(),
    });
    let bytes = serde_json::to_vec(&manifest)
        .map_err(|_| DecodeError::new("forensic manifest serialization"))?;
    if bytes.len() > MAX_OUTPUT_BYTES {
        return Err(DecodeError::new("forensic manifest exceeds output limit"));
    }
    fs::write(root_path.join("lifecycle-fuzz-manifest.json"), bytes)
        .map_err(|_| DecodeError::new("forensic manifest write"))
}

fn ensure_output_bound(output: &ReplayReport) -> Result<(), DecodeError> {
    let encoded =
        serde_json::to_vec(output).map_err(|_| DecodeError::new("output serialization"))?;
    if encoded.len() > MAX_OUTPUT_BYTES {
        Err(DecodeError::new("fuzz output exceeds output limit"))
    } else {
        Ok(())
    }
}

fn register_catalog(reducer: &mut Reducer) -> crate::Result<()> {
    reducer.register_capability("cap.session-root", CapabilityKind::SessionRootPidfd, 1)?;
    reducer.register_capability("cap.launchd-member", CapabilityKind::SessionMemberPidfd, 1)?;
    reducer.register_capability("cap.late-member", CapabilityKind::SessionMemberPidfd, 1)?;
    reducer.register_capability("cap.shared-lease", CapabilityKind::SharedSessionLease, 1)?;
    reducer.register_capability("cap.launchd-endpoint", CapabilityKind::RuntimeEndpoint, 1)?;
    Ok(())
}

fn event_for_op(op: BytecodeOp) -> Result<Option<Event>, DecodeError> {
    let event = match op {
        BytecodeOp::Intent(value) => Event::Intent {
            intent: intent_kind(value)?,
            transaction: "txn.fuzz".to_string(),
        },
        BytecodeOp::Barrier => Event::BarrierEntered,
        BytecodeOp::Acquire(capability) => Event::Acquire {
            id: capability_id(capability)?.to_string(),
            kind: capability_kind(capability)?,
            generation: 1,
            owner: "controller".to_string(),
        },
        BytecodeOp::Identity {
            capability,
            matches,
        } => Event::Identity {
            id: capability_id(capability)?.to_string(),
            matches,
        },
        BytecodeOp::Membership { mask, completeness } => Event::Membership {
            members: (0..3)
                .filter(|index| mask & (1 << index) != 0)
                .map(|index| CAPABILITY_IDS[index].to_string())
                .collect(),
            completeness: COMPLETENESS_NAMES
                .get(completeness as usize)
                .ok_or_else(|| DecodeError::new("membership completeness"))?
                .to_string(),
        },
        BytecodeOp::Signal {
            capability, result, ..
        } => Event::Signal {
            id: capability_id(capability)?.to_string(),
            result: signal_result(result)?,
        },
        BytecodeOp::Recovery(value) => Event::Recovery(recovery_action(value)?),
        BytecodeOp::Release(capability) => Event::Release {
            id: capability_id(capability)?.to_string(),
            owner: "controller".to_string(),
        },
        BytecodeOp::Checkpoint {
            operation,
            checkpoint,
            placement,
            result,
        } => Event::OperationCheckpoint {
            operation: OPERATION_NAMES
                .get(operation as usize)
                .ok_or_else(|| DecodeError::new("operation"))?
                .to_string(),
            checkpoint: CHECKPOINT_NAMES
                .get(checkpoint as usize)
                .ok_or_else(|| DecodeError::new("checkpoint"))?
                .to_string(),
            placement: if placement == 0 { "BEFORE" } else { "AFTER" }.to_string(),
            result: match result {
                0 => "INJECTED",
                1 => "COMPLETED",
                _ => "ERROR",
            }
            .to_string(),
        },
        BytecodeOp::MemberObserved { capability, origin } => Event::MemberObserved {
            id: capability_id(capability)?.to_string(),
            origin: ORIGIN_NAMES
                .get(origin as usize)
                .ok_or_else(|| DecodeError::new("member origin"))?
                .to_string(),
        },
        BytecodeOp::Fault(value) => Event::Fault {
            name: FAULT_NAMES
                .get(value as usize)
                .ok_or_else(|| DecodeError::new("fault"))?
                .to_string(),
        },
        BytecodeOp::Terminal(value) => Event::Terminal(outcome(value)?),
        BytecodeOp::Advance(_) | BytecodeOp::Noop => return Ok(None),
    };
    Ok(Some(event))
}

fn capability_id(index: u8) -> Result<&'static str, DecodeError> {
    CAPABILITY_IDS
        .get(index as usize)
        .copied()
        .ok_or_else(|| DecodeError::new("capability index"))
}

fn capability_kind(index: u8) -> Result<CapabilityKind, DecodeError> {
    Ok(match index {
        0 => CapabilityKind::SessionRootPidfd,
        1 | 2 => CapabilityKind::SessionMemberPidfd,
        3 => CapabilityKind::SharedSessionLease,
        4 => CapabilityKind::RuntimeEndpoint,
        _ => return Err(DecodeError::new("capability kind")),
    })
}

fn stable_state(value: u8) -> Result<StableState, DecodeError> {
    Ok(match value {
        0 => StableState::Uninitialized,
        1 => StableState::Ready,
        2 => StableState::Running,
        3 => StableState::Draining,
        4 => StableState::Stopped,
        5 => StableState::Corrupt,
        _ => return Err(DecodeError::new("stable state")),
    })
}

fn intent_kind(value: u8) -> Result<IntentKind, DecodeError> {
    Ok(match value {
        0 => IntentKind::None,
        1 => IntentKind::CreatePrefix,
        2 => IntentKind::StartSession,
        3 => IntentKind::RequestShutdown,
        4 => IntentKind::ReleaseSession,
        5 => IntentKind::RecreatePrefix,
        6 => IntentKind::Recover,
        _ => return Err(DecodeError::new("intent")),
    })
}

fn recovery_action(value: u8) -> Result<RecoveryAction, DecodeError> {
    Ok(match value {
        0 => RecoveryAction::Initialize,
        1 => RecoveryAction::Noop,
        2 => RecoveryAction::RollbackUninitialized,
        3 => RecoveryAction::RollbackReady,
        4 => RecoveryAction::RollbackRunning,
        5 => RecoveryAction::RollbackStopped,
        6 => RecoveryAction::CompletePublish,
        7 => RecoveryAction::CompleteCleanup,
        8 => RecoveryAction::DrainToReady,
        9 => RecoveryAction::ContinueDrain,
        10 => RecoveryAction::FailClosed,
        11 => RecoveryAction::Quarantine,
        _ => return Err(DecodeError::new("recovery action")),
    })
}

fn signal_result(value: u8) -> Result<SignalResult, DecodeError> {
    Ok(match value {
        0 => SignalResult::Sent,
        1 => SignalResult::Gone,
        2 => SignalResult::Rejected,
        3 => SignalResult::Deadline,
        _ => return Err(DecodeError::new("signal result")),
    })
}

fn outcome(value: u8) -> Result<Outcome, DecodeError> {
    Ok(match value {
        0 => Outcome::Success,
        1 => Outcome::FailClosed,
        2 => Outcome::Timeout,
        3 => Outcome::Recovered,
        _ => return Err(DecodeError::new("terminal outcome")),
    })
}

fn op_tag(op: BytecodeOp) -> u8 {
    match op {
        BytecodeOp::Intent(_) => 0,
        BytecodeOp::Barrier => 1,
        BytecodeOp::Acquire(_) => 2,
        BytecodeOp::Identity { .. } => 3,
        BytecodeOp::Membership { .. } => 4,
        BytecodeOp::Signal { .. } => 5,
        BytecodeOp::Recovery(_) => 6,
        BytecodeOp::Release(_) => 7,
        BytecodeOp::Checkpoint { .. } => 8,
        BytecodeOp::MemberObserved { .. } => 9,
        BytecodeOp::Fault(_) => 10,
        BytecodeOp::Terminal(_) => 11,
        BytecodeOp::Advance(_) => 12,
        BytecodeOp::Noop => 13,
    }
}

fn op_name(op: BytecodeOp) -> &'static str {
    match op {
        BytecodeOp::Intent(_) => "intent",
        BytecodeOp::Barrier => "barrier",
        BytecodeOp::Acquire(_) => "acquire",
        BytecodeOp::Identity { .. } => "identity",
        BytecodeOp::Membership { .. } => "membership",
        BytecodeOp::Signal { .. } => "signal",
        BytecodeOp::Recovery(_) => "recovery",
        BytecodeOp::Release(_) => "release",
        BytecodeOp::Checkpoint { .. } => "checkpoint",
        BytecodeOp::MemberObserved { .. } => "member-observed",
        BytecodeOp::Fault(_) => "fault",
        BytecodeOp::Terminal(_) => "terminal",
        BytecodeOp::Advance(_) => "advance-time",
        BytecodeOp::Noop => "noop",
    }
}

fn snapshot_code(snapshot: &crate::state::StateSnapshot) -> u16 {
    let stable = match snapshot.stable {
        StableState::Uninitialized => 0,
        StableState::Ready => 1,
        StableState::Running => 2,
        StableState::Draining => 3,
        StableState::Stopped => 4,
        StableState::Corrupt => 5,
    };
    let journal = match snapshot.journal {
        crate::state::JournalPhase::None => 0,
        crate::state::JournalPhase::Prepare => 1,
        crate::state::JournalPhase::Publish => 2,
        crate::state::JournalPhase::Cleanup => 3,
        crate::state::JournalPhase::Commit => 4,
        crate::state::JournalPhase::Abort => 5,
    };
    stable * 8 + journal
}

fn outcome_name(outcome: Outcome) -> &'static str {
    match outcome {
        Outcome::Success => "SUCCESS",
        Outcome::FailClosed => "FAIL_CLOSED",
        Outcome::Timeout => "TIMEOUT",
        Outcome::Recovered => "RECOVERED",
    }
}

fn recovery_code(status: RecoveryStatus) -> u16 {
    match status {
        RecoveryStatus::Clean => 0,
        RecoveryStatus::ForensicRequired => 1,
        RecoveryStatus::Unsafe => 2,
        RecoveryStatus::Undetected => 3,
    }
}

fn source_identity() -> Value {
    json!({
        "repository": "darling-workspace",
        "module": "lifecycle-fuzz",
        "explorer_commit": EXPLORER_SOURCE_COMMIT,
        "explorer_tree": EXPLORER_SOURCE_TREE,
        "explorer_identity_role": "provenance-anchor-only",
        "fuzz_source_sha256": env!("LIFECYCLE_FUZZ_SOURCE_SHA256"),
        "fuzz_target_sha256": env!("LIFECYCLE_FUZZ_TARGET_SHA256"),
        "semantic_closure_authoritative": true,
        "semantic_closure_scope": "reducer-explorer-policy-schema-contract-locks",
        "semantic_closure_sha256": env!("LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_SHA256"),
        "semantic_closure": serde_json::from_str::<Value>(env!("LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_JSON"))
            .expect("semantic closure identity is valid JSON"),
        "workspace_head": env!("LIFECYCLE_FUZZ_WORKSPACE_HEAD"),
        "bytecode_version": BYTECODE_VERSION,
    })
}

fn coverage_hash(edges: &BTreeSet<u16>) -> String {
    let mut hash = 0xcbf29ce484222325u64;
    for edge in edges {
        hash ^= *edge as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn operation_schedule_digest(ops: &[BytecodeOp]) -> u64 {
    ops.iter().fold(0xcbf29ce484222325u64, |hash, op| {
        hash.wrapping_mul(0x100000001b3) ^ op_tag(*op) as u64 ^ op_name(*op).len() as u64
    })
}

fn truncate_error(error: &str) -> String {
    error.chars().take(MAX_ERROR_BYTES).collect()
}

fn hex_encode(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut result = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        result.push(HEX[(byte >> 4) as usize] as char);
        result.push(HEX[(byte & 0x0f) as usize] as char);
    }
    result
}

fn standard_prefix() -> Vec<BytecodeOp> {
    vec![
        BytecodeOp::Intent(3),
        BytecodeOp::Barrier,
        BytecodeOp::Acquire(0),
        BytecodeOp::Identity {
            capability: 0,
            matches: true,
        },
    ]
}

fn golden_program(name: &str) -> Option<ScenarioBytecode> {
    let mut ops = standard_prefix();
    match name {
        "shared-session-signal-gone" => {
            ops.extend([
                BytecodeOp::Signal {
                    capability: 0,
                    signal: 0,
                    result: 1,
                },
                BytecodeOp::Terminal(3),
            ]);
        }
        "shared-session-signal-rejected" => {
            ops.extend([
                BytecodeOp::Signal {
                    capability: 0,
                    signal: 0,
                    result: 2,
                },
                BytecodeOp::Recovery(9),
                BytecodeOp::Terminal(3),
            ]);
        }
        "shared-session-retained-holder-timeout" => {
            ops.extend([
                BytecodeOp::Acquire(1),
                BytecodeOp::Acquire(3),
                BytecodeOp::Membership {
                    mask: 0b0000_0011,
                    completeness: 0,
                },
                BytecodeOp::Identity {
                    capability: 1,
                    matches: true,
                },
                BytecodeOp::Identity {
                    capability: 3,
                    matches: true,
                },
                BytecodeOp::Signal {
                    capability: 0,
                    signal: 0,
                    result: 0,
                },
                BytecodeOp::Release(0),
                BytecodeOp::Release(1),
                BytecodeOp::Fault(5),
                BytecodeOp::Recovery(9),
                BytecodeOp::Terminal(2),
            ]);
        }
        "shared-session-pid-reuse" | "session-root-exit-before-snapshot" => {
            ops[3] = BytecodeOp::Identity {
                capability: 0,
                matches: false,
            };
            ops.extend([BytecodeOp::Recovery(4), BytecodeOp::Terminal(1)]);
        }
        "shared-session-late-fork" => {
            ops.extend([
                BytecodeOp::Acquire(1),
                BytecodeOp::Membership {
                    mask: 0b0000_0011,
                    completeness: 1,
                },
                BytecodeOp::MemberObserved {
                    capability: 2,
                    origin: 2,
                },
                BytecodeOp::Fault(4),
                BytecodeOp::Recovery(4),
                BytecodeOp::Terminal(1),
            ]);
        }
        _ => return None,
    }
    Some(ScenarioBytecode {
        mode: FuzzMode::Reducer,
        seed: stable_seed(name),
        boundary: 0,
        interleaving: 0,
        initial_stable: 2,
        ops,
    })
}

fn stable_seed(name: &str) -> u64 {
    name.bytes().fold(0xcbf29ce484222325u64, |hash, byte| {
        hash.wrapping_mul(0x100000001b3) ^ byte as u64
    })
}

const FORENSIC_CASES: &[(u8, u8, &str)] = &[
    (5, 3, "after-cleanup-inode-aba"),
    (5, 4, "after-cleanup-partial-publication"),
    (5, 6, "after-cleanup-cleanup-failure"),
    (11, 3, "after-identity-inode-aba"),
    (11, 4, "after-identity-partial-publication"),
    (11, 6, "after-identity-cleanup-failure"),
    (12, 3, "before-signal-inode-aba"),
    (12, 6, "before-signal-cleanup-failure"),
    (8, 1, "before-barrier-orphan-cgroup"),
    (8, 2, "before-barrier-post-deadline-signal"),
    (9, 1, "after-barrier-orphan-cgroup"),
    (9, 2, "after-barrier-post-deadline-signal"),
    (10, 1, "before-identity-orphan-cgroup"),
    (10, 2, "before-identity-post-deadline-signal"),
    (11, 1, "after-identity-orphan-cgroup"),
    (11, 2, "after-identity-post-deadline-signal"),
    (12, 1, "before-signal-orphan-cgroup"),
    (12, 2, "before-signal-post-deadline-signal"),
    (13, 1, "after-signal-orphan-cgroup"),
    (13, 2, "after-signal-post-deadline-signal"),
    (14, 1, "before-endpoint-orphan-cgroup"),
    (14, 2, "before-endpoint-post-deadline-signal"),
    (15, 1, "after-endpoint-orphan-cgroup"),
    (15, 2, "after-endpoint-post-deadline-signal"),
];

pub struct CorpusSeed {
    pub name: &'static str,
    pub category: &'static str,
    pub provenance: &'static str,
    pub program: ScenarioBytecode,
}

struct HistoricalSeed {
    name: &'static str,
    provenance: &'static str,
    program: ScenarioBytecode,
    bad_arm: HistoricalBadArm,
}

struct HistoricalBadArm {
    program: ScenarioBytecode,
    expected_invariants: &'static [&'static str],
    expected_class: &'static str,
}

fn historical_bad_arm(name: &str, index: u8) -> HistoricalBadArm {
    let (ops, expected_invariants, expected_class): (
        Vec<BytecodeOp>,
        &'static [&'static str],
        &'static str,
    ) = match name {
        // A post-move exact mutation reports an inode replacement but the
        // accepted trace omits the required identity revalidation.
        "historical-inode-aba" => (
            vec![
                BytecodeOp::Intent(3),
                BytecodeOp::Barrier,
                BytecodeOp::Acquire(0),
                BytecodeOp::Identity {
                    capability: 0,
                    matches: true,
                },
                BytecodeOp::Checkpoint {
                    operation: 2,
                    checkpoint: 4,
                    placement: 1,
                    result: 2,
                },
                BytecodeOp::Terminal(3),
            ],
            &["inode-aba-revalidated", "operation-failure-recovered"],
            "inode-aba-after-exact-mutation",
        ),
        // PID reuse is an accepted identity fault.  No stale capability is
        // used: the bad arm specifically omits the required revalidation.
        "historical-pid-reuse" => (
            vec![
                BytecodeOp::Intent(3),
                BytecodeOp::Barrier,
                BytecodeOp::Acquire(0),
                BytecodeOp::Identity {
                    capability: 0,
                    matches: true,
                },
                BytecodeOp::Fault(3),
                BytecodeOp::Terminal(3),
            ],
            &["pid-reuse-revalidated"],
            "pid-reuse-after-identity-mismatch",
        ),
        // The closed membership snapshot is followed by a real late-member
        // observation and a late-fork fault, with no recovery action.
        "historical-late-fork" => (
            vec![
                BytecodeOp::Intent(3),
                BytecodeOp::Barrier,
                BytecodeOp::Acquire(0),
                BytecodeOp::Acquire(1),
                BytecodeOp::Membership {
                    mask: 0b11,
                    completeness: 1,
                },
                BytecodeOp::MemberObserved {
                    capability: 2,
                    origin: 2,
                },
                BytecodeOp::Fault(4),
                BytecodeOp::Terminal(3),
            ],
            &["late-fork-closes-snapshot"],
            "late-fork-after-closed-snapshot",
        ),
        // Deadline is a typed signal result.  The trace is accepted but does
        // not provide the required deadline recovery evidence.
        "historical-deadline" => (
            vec![
                BytecodeOp::Acquire(0),
                BytecodeOp::Identity {
                    capability: 0,
                    matches: true,
                },
                BytecodeOp::Intent(3),
                BytecodeOp::Signal {
                    capability: 0,
                    signal: 0,
                    result: 3,
                },
                BytecodeOp::Terminal(3),
            ],
            &["deadline-recovered"],
            "deadline-before-drain",
        ),
        // Rejected is a typed signal outcome, not a reducer event rejection;
        // the bad arm omits the recovery that would close signal-failure.
        "historical-rejected-signal" => (
            vec![
                BytecodeOp::Acquire(0),
                BytecodeOp::Identity {
                    capability: 0,
                    matches: true,
                },
                BytecodeOp::Intent(3),
                BytecodeOp::Barrier,
                BytecodeOp::Signal {
                    capability: 0,
                    signal: 0,
                    result: 2,
                },
                BytecodeOp::Terminal(3),
            ],
            &["signal-failure-recovered"],
            "rejected-signal-before-drain",
        ),
        // Root exit is an accepted fault.  Membership traversal is not
        // attempted after the root disappears, but the required root-GONE
        // closure evidence is intentionally absent in this RED arm.
        "historical-root-gone" => (
            vec![
                BytecodeOp::Intent(3),
                BytecodeOp::Barrier,
                BytecodeOp::Acquire(0),
                BytecodeOp::Identity {
                    capability: 0,
                    matches: true,
                },
                BytecodeOp::Fault(2),
                BytecodeOp::Terminal(3),
            ],
            &["root-gone-membership-revalidated"],
            "root-gone-before-membership-snapshot",
        ),
        // An operation reports an accepted error, but no recovery closes its
        // registered operation obligation.
        "historical-cleanup-failure" => (
            vec![
                BytecodeOp::Intent(3),
                BytecodeOp::Checkpoint {
                    operation: 0,
                    checkpoint: 3,
                    placement: 1,
                    result: 2,
                },
                BytecodeOp::Terminal(3),
            ],
            &["operation-failure-recovered"],
            "cleanup-failure-before-recovery",
        ),
        // Quarantine replacement is a distinct post-move failure.  The
        // operation and quarantine obligations are both intentionally left
        // open so the RED is tied to that exact recovery class.
        "historical-quarantine-replacement" => (
            vec![
                BytecodeOp::Intent(3),
                BytecodeOp::Barrier,
                BytecodeOp::Checkpoint {
                    operation: 1,
                    checkpoint: 5,
                    placement: 1,
                    result: 2,
                },
                BytecodeOp::Terminal(3),
            ],
            &["operation-failure-recovered", "quarantine-recovery"],
            "quarantine-replacement-after-move",
        ),
        _ => panic!("unknown historical bad arm: {name}"),
    };
    HistoricalBadArm {
        program: ScenarioBytecode {
            mode: FuzzMode::Reducer,
            seed: 0x4849_5354_0000_0000 | index as u64,
            boundary: 0,
            interleaving: 0,
            initial_stable: 2,
            ops,
        },
        expected_invariants,
        expected_class,
    }
}

fn historical_seeds() -> Vec<HistoricalSeed> {
    let selectors = [
        (
            "historical-inode-aba",
            "rootless-prefix-lifecycle;deploy;inode-ABA",
            5,
            3,
        ),
        ("historical-pid-reuse", "rootless-shutdown;PID-reuse", 10, 9),
        ("historical-late-fork", "rootless-shutdown;late-fork", 12, 7),
        ("historical-deadline", "rootless-shutdown;deadline", 12, 2),
        (
            "historical-rejected-signal",
            "operation-boundary;split-lock;rejected-signal",
            0,
            0,
        ),
        (
            "historical-root-gone",
            "E-UNION;stale-index;root-GONE",
            0,
            0,
        ),
        (
            "historical-cleanup-failure",
            "E-UNION;deploy;cleanup-failure",
            5,
            6,
        ),
        (
            "historical-quarantine-replacement",
            "verifier-regression;E-UNION;quarantine-replacement",
            11,
            6,
        ),
    ];
    selectors
        .into_iter()
        .enumerate()
        .map(|(index, (name, provenance, boundary, interleaving))| {
            let program = if name == "historical-rejected-signal" {
                let mut value =
                    golden_program("shared-session-signal-rejected").expect("rejected seed");
                // Keep terminal strictly last.  The historical fixture is
                // unique by its immutable seed, not by appending ignored
                // operations after terminal.
                value.seed = stable_seed(name);
                value
            } else if name == "historical-root-gone" {
                let mut value =
                    golden_program("session-root-exit-before-snapshot").expect("root gone seed");
                // Keep terminal strictly last; uniqueness comes from the
                // historical seed identity.
                value.seed = stable_seed(name);
                value
            } else {
                ScenarioBytecode {
                    mode: FuzzMode::Explorer,
                    seed: 7,
                    boundary,
                    interleaving,
                    initial_stable: 2,
                    ops: vec![BytecodeOp::Noop, BytecodeOp::Advance(index as u16 + 1)],
                }
            };
            HistoricalSeed {
                name,
                provenance,
                program,
                bad_arm: historical_bad_arm(name, index as u8),
            }
        })
        .collect()
}

pub fn corpus_seeds() -> Vec<CorpusSeed> {
    let golden_names = [
        "shared-session-signal-gone",
        "shared-session-signal-rejected",
        "shared-session-retained-holder-timeout",
        "shared-session-pid-reuse",
        "shared-session-late-fork",
        "session-root-exit-before-snapshot",
    ];
    let mut seeds = Vec::with_capacity(38);
    for name in golden_names {
        seeds.push(CorpusSeed {
            name,
            category: "golden-trace",
            provenance: "golden-trace;accepted-reducer-fixture",
            program: golden_program(name).expect("golden program registry is complete"),
        });
    }
    for (boundary, interleaving, name) in FORENSIC_CASES {
        seeds.push(CorpusSeed {
            name,
            category: "forensic-explorer",
            provenance: "explorer;FORENSIC_REQUIRED;deterministic-selector",
            program: ScenarioBytecode {
                mode: FuzzMode::Explorer,
                seed: 7,
                boundary: *boundary,
                interleaving: *interleaving,
                initial_stable: 2,
                ops: vec![BytecodeOp::Noop],
            },
        });
    }
    for historical in historical_seeds() {
        seeds.push(CorpusSeed {
            name: historical.name,
            category: "historical-seed",
            provenance: historical.provenance,
            program: historical.program,
        });
    }
    seeds
}

pub fn corpus_seed_hex(name: &str) -> Result<String, DecodeError> {
    corpus_seeds()
        .into_iter()
        .find(|seed| seed.name == name)
        .ok_or_else(|| DecodeError::new("unknown corpus seed"))?
        .program
        .hex()
}

pub fn materialize_corpus(directory: &Path) -> Result<usize, DecodeError> {
    fs::create_dir_all(directory).map_err(|_| DecodeError::new("corpus directory create"))?;
    let mut count = 0usize;
    for seed in corpus_seeds() {
        let filename = seed.name.replace('/', "_");
        let path = directory.join(filename);
        fs::write(path, seed.program.encode()?)
            .map_err(|_| DecodeError::new("corpus seed write"))?;
        count += 1;
    }
    Ok(count)
}

#[derive(Debug, Serialize)]
pub struct CorpusVerification {
    pub corpus_count: usize,
    pub golden_count: usize,
    pub forensic_count: usize,
    pub historical_count: usize,
    pub historical_replayed: usize,
    pub historical_bad_arm_reproduced: usize,
    pub historical_bad_arm_unique: usize,
    pub unique_programs: usize,
    pub historical_provenance: Vec<String>,
    pub historical_bad_violations: Vec<Value>,
    pub forensic_reproduced: usize,
    pub forensic_roots: Vec<String>,
    pub source_identity: Value,
    pub names: Vec<String>,
}

pub fn verify_corpus() -> Result<CorpusVerification, DecodeError> {
    let seeds = corpus_seeds();
    let mut names = Vec::with_capacity(seeds.len());
    let mut golden_count = 0;
    let mut forensic_count = 0;
    let mut historical_count = 0;
    let mut encodings = BTreeSet::new();
    for seed in &seeds {
        let bytes = seed.program.encode()?;
        let decoded = ScenarioBytecode::decode(&bytes)?;
        if decoded != seed.program {
            return Err(DecodeError::new("corpus decode/encode mismatch"));
        }
        if !encodings.insert(bytes) {
            return Err(DecodeError::new("corpus contains duplicate bytecode"));
        }
        if seed.category == "forensic-explorer" {
            forensic_count += 1;
        } else if seed.category == "golden-trace" {
            golden_count += 1;
        } else {
            historical_count += 1;
        }
        names.push(seed.name.to_string());
    }
    let historical = historical_seeds();
    let mut historical_replayed = 0usize;
    let mut historical_bad_arm_reproduced = 0usize;
    let mut historical_bad_arm_encodings = BTreeSet::new();
    let mut historical_provenance = Vec::with_capacity(historical.len());
    let mut historical_bad_violations = Vec::with_capacity(historical.len());
    for seed in &historical {
        historical_bad_arm_encodings.insert(seed.bad_arm.program.encode()?);
        let good = replay_program(&seed.program)?;
        if good.status == "PANIC" || good.status == "DECODE_REJECTED" {
            return Err(DecodeError::new("historical seed replay failed"));
        }
        let bad = replay_program(&seed.bad_arm.program)?;
        let expected_missing: BTreeSet<&str> =
            seed.bad_arm.expected_invariants.iter().copied().collect();
        let actual_missing: BTreeSet<&str> = bad
            .failure_fingerprint
            .missing_invariants
            .iter()
            .map(String::as_str)
            .collect();
        if bad.status != "INVARIANT_VIOLATION" || actual_missing != expected_missing {
            return Err(DecodeError::new(&format!(
                "historical bad arm {} was not reproduced: status={} expected={:?} missing={:?}",
                seed.name, bad.status, expected_missing, actual_missing,
            )));
        }
        historical_replayed += 1;
        historical_bad_arm_reproduced += 1;
        historical_provenance.push(format!("{}:{}", seed.name, seed.provenance));
        historical_bad_violations.push(json!({
            "name": seed.name,
            "expected_invariants": seed.bad_arm.expected_invariants,
            "expected_class": seed.bad_arm.expected_class,
            "actual": bad.failure_fingerprint,
            "program": bad.minimized_bytecode_hex,
        }));
        if let Some(path) = good.forensic_roots.first() {
            remove_owned_forensic_root(path)?;
        }
    }
    let report = explore(7, ExplorerBudget::default())
        .map_err(|error| DecodeError::new(&error.to_string()))?;
    let mut forensic_roots = Vec::with_capacity(FORENSIC_CASES.len());
    let mut reproduced = 0usize;
    for (_, _, name) in FORENSIC_CASES {
        let scenario = report
            .scenarios
            .iter()
            .find(|scenario| {
                scenario.scenario == *name
                    && scenario.recovery_status == RecoveryStatus::ForensicRequired
            })
            .ok_or_else(|| {
                DecodeError::new(&format!("forensic scenario {name} was not reproduced"))
            })?;
        if let Some(path) = scenario.forensic_root.as_deref() {
            write_forensic_manifest(scenario, path)?;
            forensic_roots.push(path.to_string());
        }
        reproduced += 1;
    }
    if golden_count != 6 || forensic_count != 24 || historical_count != 8 || reproduced != 24 {
        return Err(DecodeError::new(
            "corpus inventory or forensic reproduction mismatch",
        ));
    }
    if forensic_roots.len() != 8 {
        return Err(DecodeError::new("forensic manifest inventory mismatch"));
    }
    Ok(CorpusVerification {
        corpus_count: seeds.len(),
        golden_count,
        forensic_count,
        historical_count,
        forensic_reproduced: reproduced,
        historical_replayed,
        historical_bad_arm_reproduced,
        historical_bad_arm_unique: historical_bad_arm_encodings.len(),
        unique_programs: encodings.len(),
        historical_provenance,
        historical_bad_violations,
        forensic_roots,
        source_identity: source_identity(),
        names,
    })
}

#[derive(Debug, Serialize)]
pub struct SmokeReport {
    pub status: &'static str,
    pub max_cases: usize,
    pub cases: usize,
    pub unique_coverage: usize,
    pub accepted: usize,
    pub rejected: usize,
    pub incomplete: usize,
    pub budget_exceeded: usize,
    pub invariant_failures: usize,
    /// Explorer-mode programs are observed by the real boundary/explorer and
    /// have a distinct terminal status from reducer accept/reject outcomes.
    /// Keep this count explicit so the smoke accounting is exhaustive rather
    /// than silently dropping an otherwise valid status.
    pub explorer_observed: usize,
    pub mutated_cases: usize,
    pub corpus: CorpusVerification,
    pub source_identity: Value,
}

pub fn run_smoke(max_cases: usize) -> Result<SmokeReport, DecodeError> {
    if max_cases == 0 || max_cases > 256 {
        return Err(DecodeError::new("smoke case budget"));
    }
    let corpus = verify_corpus()?;
    let seeds = corpus_seeds();
    let mut cases = 0;
    let mut accepted = 0;
    let mut rejected = 0;
    let mut incomplete = 0;
    let mut budget_exceeded = 0;
    let mut invariant_failures = 0;
    let mut explorer_observed = 0;
    let mut mutated_cases = 0;
    let mut coverage = BTreeSet::new();
    for seed in seeds
        .iter()
        .filter(|seed| seed.category != "forensic-explorer")
    {
        if cases >= max_cases {
            break;
        }
        let report = replay_program(&seed.program)?;
        cases += 1;
        coverage.extend(report.coverage_edges);
        match report.status.as_str() {
            "ACCEPTED" => accepted += 1,
            "REJECTED" => rejected += 1,
            "INCOMPLETE" => incomplete += 1,
            "BUDGET_EXCEEDED" => budget_exceeded += 1,
            "INVARIANT_VIOLATION" => invariant_failures += 1,
            "EXPLORER_OBSERVED" => explorer_observed += 1,
            _ => {}
        }
    }
    // Deterministic coverage-guided mutations stay bounded and use the same
    // reducer oracle; no independent expected-state model is introduced.
    for seed in seeds.iter().filter(|seed| seed.category == "golden-trace") {
        if cases >= max_cases {
            break;
        }
        for mutation in deterministic_mutations(&seed.program) {
            if cases >= max_cases {
                break;
            }
            let report = replay_program(&mutation)?;
            cases += 1;
            mutated_cases += 1;
            coverage.extend(report.coverage_edges);
            match report.status.as_str() {
                "ACCEPTED" => accepted += 1,
                "REJECTED" => rejected += 1,
                "INCOMPLETE" => incomplete += 1,
                "BUDGET_EXCEEDED" => budget_exceeded += 1,
                "INVARIANT_VIOLATION" => invariant_failures += 1,
                "EXPLORER_OBSERVED" => explorer_observed += 1,
                _ => {}
            }
        }
    }
    Ok(SmokeReport {
        status: "FUZZ_SMOKE_VALID",
        max_cases,
        cases,
        unique_coverage: coverage.len(),
        accepted,
        rejected,
        incomplete,
        budget_exceeded,
        invariant_failures,
        explorer_observed,
        mutated_cases,
        corpus,
        source_identity: source_identity(),
    })
}

fn deterministic_mutations(program: &ScenarioBytecode) -> Vec<ScenarioBytecode> {
    let mut mutations = Vec::new();
    for bit in [1u64, 2, 4] {
        let mut mutated = program.clone();
        mutated.seed ^= bit;
        mutations.push(mutated);
    }
    for (index, op) in program.ops.iter().enumerate() {
        if let BytecodeOp::Advance(value) = op {
            let mut mutated = program.clone();
            mutated.ops[index] = BytecodeOp::Advance(value.saturating_add(1));
            mutations.push(mutated);
            break;
        }
    }
    if program.ops.len() < MAX_OPS {
        let mut appended = program.clone();
        appended.ops.push(BytecodeOp::Noop);
        mutations.push(appended);
    }
    mutations
}

fn fuzz_report_for_decode_error(error: DecodeError, input_bytes: usize) -> ReplayReport {
    ReplayReport {
        status: "DECODE_REJECTED".to_string(),
        mode: "unknown".to_string(),
        seed: 0,
        event_count: 0,
        virtual_time_ns: 0,
        recovery_steps: 0,
        schedule_steps: 0,
        terminal: None,
        unresolved_obligations: Vec::new(),
        errors: vec![truncate_error(&error.to_string())],
        rejected_events: 0,
        coverage_edges: Vec::new(),
        coverage_hash: coverage_hash(&BTreeSet::new()),
        minimized_bytecode_hex: None,
        minimized_replay_trace: Vec::new(),
        scenario: None,
        recovery_status: None,
        forensic_roots: Vec::new(),
        source_identity: source_identity(),
        resource_census: json!({
            "input_bytes": input_bytes,
            "input_limit": MAX_INPUT_BYTES,
            "events": 0,
            "capability_limit": MAX_CAPABILITIES,
            "virtual_time_limit_ns": MODEL_MAX_VIRTUAL_TIME_NS,
            "schedule_limit": MAX_SCHEDULE_STEPS,
            "recovery_limit": MODEL_MAX_RECOVERY_STEPS,
            "filesystem_roots_created": 0,
        }),
        reproduction_command: "lifecycle-fuzz --replay-hex <rejected-input>".to_string(),
        failure_fingerprint: failure_fingerprint(
            "DECODE_REJECTED",
            &[truncate_error(&error.to_string())],
            None,
            None,
        ),
    }
}

pub fn safe_replay(input: &[u8]) -> ReplayReport {
    match std::panic::catch_unwind(|| fuzz_one(input)) {
        Ok(Ok(report)) => report,
        Ok(Err(error)) => fuzz_report_for_decode_error(error, input.len()),
        Err(_) => ReplayReport {
            status: "PANIC".to_string(),
            mode: "unknown".to_string(),
            seed: 0,
            event_count: 0,
            virtual_time_ns: 0,
            recovery_steps: 0,
            schedule_steps: 0,
            terminal: None,
            unresolved_obligations: Vec::new(),
            errors: vec!["panic while decoding or replaying input".to_string()],
            rejected_events: 0,
            coverage_edges: Vec::new(),
            coverage_hash: coverage_hash(&BTreeSet::new()),
            minimized_bytecode_hex: None,
            minimized_replay_trace: Vec::new(),
            scenario: None,
            recovery_status: None,
            forensic_roots: Vec::new(),
            source_identity: source_identity(),
            resource_census: json!({
                "input_bytes": input.len(),
                "input_limit": MAX_INPUT_BYTES,
                "events": 0,
                "capability_limit": MAX_CAPABILITIES,
                "virtual_time_limit_ns": MODEL_MAX_VIRTUAL_TIME_NS,
                "schedule_limit": MAX_SCHEDULE_STEPS,
                "recovery_limit": MODEL_MAX_RECOVERY_STEPS,
                "filesystem_roots_created": 0,
            }),
            reproduction_command: "lifecycle-fuzz --replay-hex <panic-input>".to_string(),
            failure_fingerprint: FailureFingerprint::empty("PANIC"),
        },
    }
}

trait FailureBoundaryIndex {
    fn from_index(index: u8) -> Option<FailureBoundary>;
}

impl FailureBoundaryIndex for FailureBoundary {
    fn from_index(index: u8) -> Option<FailureBoundary> {
        match index {
            0 => Some(FailureBoundary::BeforePrepare),
            1 => Some(FailureBoundary::AfterPrepare),
            2 => Some(FailureBoundary::BeforePublish),
            3 => Some(FailureBoundary::AfterPublish),
            4 => Some(FailureBoundary::BeforeCleanup),
            5 => Some(FailureBoundary::AfterCleanup),
            6 => Some(FailureBoundary::BeforeCommit),
            7 => Some(FailureBoundary::AfterCommit),
            8 => Some(FailureBoundary::BeforeBarrier),
            9 => Some(FailureBoundary::AfterBarrier),
            10 => Some(FailureBoundary::BeforeIdentity),
            11 => Some(FailureBoundary::AfterIdentity),
            12 => Some(FailureBoundary::BeforeSignal),
            13 => Some(FailureBoundary::AfterSignal),
            14 => Some(FailureBoundary::BeforeEndpoint),
            15 => Some(FailureBoundary::AfterEndpoint),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bytecode_round_trip_is_canonical() {
        let program = golden_program("shared-session-signal-gone").unwrap();
        let bytes = program.encode().unwrap();
        assert_eq!(ScenarioBytecode::decode(&bytes).unwrap(), program);
        assert_eq!(program.hex().unwrap(), hex_encode(&bytes));
    }

    #[test]
    fn malformed_inputs_are_bounded_and_rejected() {
        assert!(ScenarioBytecode::decode(&[]).is_err());
        let program = golden_program("shared-session-signal-gone").unwrap();
        let bytes = program.encode().unwrap();
        assert!(ScenarioBytecode::decode(&bytes[..bytes.len() - 1]).is_err());
        assert!(ScenarioBytecode::decode(&vec![0u8; MAX_INPUT_BYTES + 1]).is_err());
        let mut trailing = bytes;
        trailing.push(0);
        assert!(ScenarioBytecode::decode(&trailing).is_err());
    }

    #[test]
    fn corpus_inventory_is_fixed_and_forensic_matrix_is_named() {
        let seeds = corpus_seeds();
        assert_eq!(seeds.len(), 38);
        assert_eq!(
            seeds
                .iter()
                .filter(|seed| seed.category == "golden-trace")
                .count(),
            6
        );
        assert_eq!(
            seeds
                .iter()
                .filter(|seed| seed.category == "forensic-explorer")
                .count(),
            24
        );
        assert_eq!(
            seeds
                .iter()
                .filter(|seed| seed.category == "historical-seed")
                .count(),
            8
        );
        assert!(seeds
            .iter()
            .any(|seed| seed.name == "historical-quarantine-replacement"));
    }

    #[test]
    fn reducer_replay_never_requires_recorded_state_after() {
        let program = golden_program("shared-session-signal-rejected").unwrap();
        let report = replay_program(&program).unwrap();
        assert_eq!(report.status, "ACCEPTED");
        assert_eq!(report.terminal.as_deref(), Some("RECOVERED"));
    }

    #[test]
    fn invariant_oracle_rejects_success_with_live_shared_lease() {
        let program = ScenarioBytecode {
            mode: FuzzMode::Reducer,
            seed: 41,
            boundary: 0,
            interleaving: 0,
            initial_stable: 2,
            ops: vec![BytecodeOp::Acquire(3), BytecodeOp::Terminal(0)],
        };
        let report = replay_program(&program).unwrap();
        assert_eq!(report.status, "INVARIANT_VIOLATION");
        assert!(report
            .errors
            .iter()
            .any(|error| error.contains("shared-lease-bound")));
    }

    #[test]
    fn rejected_event_is_not_reported_as_invariant_crash() {
        let mut program = golden_program("shared-session-signal-gone").unwrap();
        program.ops[0] = BytecodeOp::Acquire(0);
        let report = replay_program(&program).unwrap();
        assert_eq!(report.status, "REJECTED");
        assert!(report.rejected_events > 0);
        assert!(report
            .failure_fingerprint
            .error_classes
            .contains(&"event-rejection".to_string()));
    }

    #[test]
    fn fuzz_target_classifies_malformed_and_rejected_inputs_without_crash() {
        let malformed = safe_replay(b"not-bytecode");
        assert_eq!(malformed.status, "DECODE_REJECTED");

        let mut program = golden_program("shared-session-signal-gone").unwrap();
        program.ops[0] = BytecodeOp::Acquire(0);
        let rejected = safe_replay(&program.encode().unwrap());
        assert_eq!(rejected.status, "REJECTED");
        assert!(rejected.rejected_events > 0);
        assert_ne!(rejected.status, "INVARIANT_VIOLATION");
    }

    #[test]
    fn terminal_is_strictly_last_for_noop_and_advance() {
        for suffix in [BytecodeOp::Noop, BytecodeOp::Advance(1)] {
            let program = ScenarioBytecode {
                mode: FuzzMode::Reducer,
                seed: 0x5445_524d_494e_414c,
                boundary: 0,
                interleaving: 0,
                initial_stable: 2,
                ops: vec![BytecodeOp::Terminal(1), suffix],
            };
            let report = replay_program(&program).expect("terminal suffix replay");
            assert_eq!(report.status, "REJECTED");
            assert!(report.rejected_events > 0);
            assert!(report
                .errors
                .iter()
                .any(|error| error.contains("event after terminal")));
        }
    }

    #[test]
    fn budget_exhaustion_cannot_be_promoted_to_invariant_crash() {
        let mut ops = standard_prefix();
        ops.push(BytecodeOp::Signal {
            capability: 0,
            signal: 0,
            result: 3,
        });
        ops.extend(std::iter::repeat_n(BytecodeOp::Advance(u16::MAX), 16));
        let report = replay_program(&ScenarioBytecode {
            mode: FuzzMode::Reducer,
            seed: 0x4255_4447_4554,
            boundary: 0,
            interleaving: 0,
            initial_stable: 2,
            ops,
        })
        .expect("budget replay");
        assert_eq!(report.status, "BUDGET_EXCEEDED");
        assert_ne!(report.status, "INVARIANT_VIOLATION");
        assert!(report
            .errors
            .iter()
            .any(|error| error.contains("virtual time budget exceeded")));
    }

    #[test]
    fn kernel_pid_reuse_requires_starttime_and_retired_pidfd_witnesses() {
        let program = golden_program("shared-session-pid-reuse").unwrap();
        let input = program.encode().unwrap();
        let common = KernelObservationCommon {
            trace_id: "shared-session-pid-reuse".to_string(),
            event_kinds: canonical_trace_spec("shared-session-pid-reuse")
                .unwrap()
                .2
                .iter()
                .map(|event| (*event).to_string())
                .collect(),
            outcome: "FAIL_CLOSED".to_string(),
            filesystem_clean: true,
            pidfd_retained: true,
        };
        let valid = KernelObservation::PidReuse {
            common: common.clone(),
            identity: "MISMATCH".to_string(),
            pid_reuse_classification: "NUMERIC_PID_REUSE_STARTTIME_MISMATCH".to_string(),
            original_pid: 100,
            original_starttime: 10,
            replacement_pid: 100,
            replacement_starttime: 11,
            retired_pidfd_signal: "ESRCH".to_string(),
            replacement_alive: true,
        };
        assert!(verify_kernel_observation(&input, &valid).is_ok());

        let mut invalid = valid;
        if let KernelObservation::PidReuse {
            replacement_starttime,
            ..
        } = &mut invalid
        {
            *replacement_starttime = 10;
        }
        assert!(verify_kernel_observation(&input, &invalid).is_err());
    }

    #[test]
    fn kernel_late_fork_requires_controller_ack_and_post_drain() {
        let program = golden_program("shared-session-late-fork").unwrap();
        let input = program.encode().unwrap();
        let common = KernelObservationCommon {
            trace_id: "shared-session-late-fork".to_string(),
            event_kinds: canonical_trace_spec("shared-session-late-fork")
                .unwrap()
                .2
                .iter()
                .map(|event| (*event).to_string())
                .collect(),
            outcome: "FAIL_CLOSED".to_string(),
            filesystem_clean: true,
            pidfd_retained: true,
        };
        let mut observation = KernelObservation::LateFork {
            common,
            late_fork: "OBSERVED_AND_REAPED".to_string(),
            late_pid: 101,
            late_starttime: 20,
            late_fork_observed_by_controller: true,
            late_fork_acknowledged: false,
            late_fork_post_drain: "GONE".to_string(),
        };
        assert!(verify_kernel_observation(&input, &observation).is_err());
        if let KernelObservation::LateFork {
            late_fork_acknowledged,
            ..
        } = &mut observation
        {
            *late_fork_acknowledged = true;
        }
        assert!(verify_kernel_observation(&input, &observation).is_ok());
    }

    #[test]
    fn minimizer_preserves_failure_fingerprint() {
        let arm = &historical_seeds()[0].bad_arm.program;
        let original = replay_reducer(arm, false).unwrap();
        let minimized = replay_reducer(arm, true).unwrap();
        assert_eq!(original.failure_fingerprint, minimized.failure_fingerprint);
        assert_eq!(minimized.status, "INVARIANT_VIOLATION");
    }

    #[test]
    fn explorer_one_preserves_selected_interleaving() {
        let program = ScenarioBytecode {
            mode: FuzzMode::Explorer,
            seed: 7,
            boundary: 5,
            interleaving: 3,
            initial_stable: 2,
            ops: vec![BytecodeOp::Noop],
        };
        let report = replay_program(&program).unwrap();
        assert_eq!(report.scenario.as_deref(), Some("after-cleanup-inode-aba"));
        assert_eq!(report.recovery_status.as_deref(), Some("FORENSIC_REQUIRED"));
        assert_eq!(report.resource_census["filesystem_roots_created"], 1);
    }

    /// Miri deliberately exercises only the semantic core.  The explorer's
    /// Linux fd-relative syscalls are covered by the ASan/TSan fuzz targets;
    /// this test keeps the Miri workload deterministic and filesystem-free.
    #[test]
    fn miri_semantic_core() {
        let reducer_seeds: Vec<_> = corpus_seeds()
            .into_iter()
            .filter(|seed| seed.program.mode == FuzzMode::Reducer)
            .collect();
        for seed in &reducer_seeds {
            let encoded = seed.program.encode().expect("reducer seed encoding");
            let decoded = ScenarioBytecode::decode(&encoded).expect("reducer seed decoding");
            let report = replay_reducer(&decoded, false).expect("reducer seed replay");
            assert_ne!(report.status, "PANIC");
            assert_ne!(report.status, "DECODE_REJECTED");
        }
        // Miri executes the complete bounded deterministic mutation fan-out
        // for every reducer seed.  The native ASan/TSan targets additionally
        // cover the Linux explorer path; no second semantic model is used.
        let mut mutation_count = 0usize;
        for seed in &reducer_seeds {
            for mutation in deterministic_mutations(&seed.program) {
                let mutation =
                    ScenarioBytecode::decode(&mutation.encode().expect("mutation encoding"))
                        .expect("mutation decoding");
                let report = replay_reducer(&mutation, false).expect("mutation replay");
                assert_ne!(report.status, "PANIC");
                assert_ne!(report.status, "DECODE_REJECTED");
                mutation_count += 1;
            }
        }
        assert!(mutation_count >= reducer_seeds.len());

        let historical = historical_seeds();
        let mut bad_reports = Vec::with_capacity(historical.len());
        for seed in &historical {
            let bad = replay_reducer(&seed.bad_arm.program, false).expect("bad arm replay");
            assert_eq!(bad.status, "INVARIANT_VIOLATION");
            let expected: BTreeSet<&str> =
                seed.bad_arm.expected_invariants.iter().copied().collect();
            let actual: BTreeSet<&str> = bad
                .failure_fingerprint
                .missing_invariants
                .iter()
                .map(String::as_str)
                .collect();
            assert_eq!(actual, expected);
            bad_reports.push(bad);
        }
        // All eight bad arms are replayed under Miri.  The minimizer is
        // exercised on a deterministic representative here; the native
        // contract minimizes each arm while preserving its exact fingerprint.
        let minimized =
            replay_reducer(&historical[0].bad_arm.program, true).expect("bad arm minimization");
        assert_eq!(
            bad_reports[0].failure_fingerprint,
            minimized.failure_fingerprint
        );
    }
}
