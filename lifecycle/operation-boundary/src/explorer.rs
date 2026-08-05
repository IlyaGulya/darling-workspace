//! Deterministic bounded explorer for `dar-4ush.3`.
//!
//! The explorer drives the accepted Rust reducer through a finite matrix of
//! failure boundaries and representative interleavings.  It does not own
//! product routing or discover host state: every input is explicit, virtual
//! time is monotonic, and every emitted trace can be replayed by the `.1`
//! schema/model and the `.2` Rust reducer.

use crate::state::{
    CapabilityKind, Event, IntentKind, JournalPhase, Outcome, RecoveryAction, Reducer,
    SignalResult, StableState, StateSnapshot, INVARIANT_REGISTRY, MODEL_MAX_EVENTS,
    MODEL_MAX_LIVE_CAPABILITIES, MODEL_MAX_RECOVERY_STEPS, MODEL_MAX_VIRTUAL_TIME_NS,
};
use crate::{
    duplicate_fd, unlink_at, Boundary, BoundaryError, Checkpoint, Clock, DirCap, ExclusiveLease,
    FaultInjector, FileIdentity, MutationState, Observer, OperationName, OperationOutcome,
    QuarantineObligation, QuiescentScope, StageObligation, VecObserver,
};
use serde::Serialize;
use serde_json::{json, Value};
use std::fmt;
use std::fs;
use std::os::fd::AsRawFd;
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::sync::{
    atomic::AtomicU64,
    atomic::{AtomicBool, Ordering},
    Arc,
};

pub const EXPLORER_VERSION: u32 = 2;
pub const EXPLORER_SOURCE_COMMIT: &str = "d02570556cac7df68459fe2e7df4f189b665b955";
pub const EXPLORER_SOURCE_TREE: &str = "1daf503705aea74c7a6a2d7b94205409d164b9b5";

static NEXT_PROBE_ROOT: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum FailureBoundary {
    BeforePrepare,
    AfterPrepare,
    BeforePublish,
    AfterPublish,
    BeforeCleanup,
    AfterCleanup,
    BeforeCommit,
    AfterCommit,
    BeforeBarrier,
    AfterBarrier,
    BeforeIdentity,
    AfterIdentity,
    BeforeSignal,
    AfterSignal,
    BeforeEndpoint,
    AfterEndpoint,
}

pub const FAILURE_BOUNDARIES: [FailureBoundary; 16] = [
    FailureBoundary::BeforePrepare,
    FailureBoundary::AfterPrepare,
    FailureBoundary::BeforePublish,
    FailureBoundary::AfterPublish,
    FailureBoundary::BeforeCleanup,
    FailureBoundary::AfterCleanup,
    FailureBoundary::BeforeCommit,
    FailureBoundary::AfterCommit,
    FailureBoundary::BeforeBarrier,
    FailureBoundary::AfterBarrier,
    FailureBoundary::BeforeIdentity,
    FailureBoundary::AfterIdentity,
    FailureBoundary::BeforeSignal,
    FailureBoundary::AfterSignal,
    FailureBoundary::BeforeEndpoint,
    FailureBoundary::AfterEndpoint,
];

/// These labels map directly to checkpoints owned by the fd-relative
/// operation boundary.  The remaining labels are reducer scenario aliases;
/// they are deliberately not advertised as process/session lifecycle seams.
pub const REAL_FAILURE_BOUNDARIES: [FailureBoundary; 8] = [
    FailureBoundary::BeforePrepare,
    FailureBoundary::AfterPrepare,
    FailureBoundary::BeforePublish,
    FailureBoundary::AfterPublish,
    FailureBoundary::BeforeCleanup,
    FailureBoundary::AfterCleanup,
    FailureBoundary::BeforeCommit,
    FailureBoundary::AfterCommit,
];

pub const REDUCER_ONLY_FAILURE_BOUNDARIES: [FailureBoundary; 8] = [
    FailureBoundary::BeforeBarrier,
    FailureBoundary::AfterBarrier,
    FailureBoundary::BeforeIdentity,
    FailureBoundary::AfterIdentity,
    FailureBoundary::BeforeSignal,
    FailureBoundary::AfterSignal,
    FailureBoundary::BeforeEndpoint,
    FailureBoundary::AfterEndpoint,
];

impl FailureBoundary {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::BeforePrepare => "before-prepare",
            Self::AfterPrepare => "after-prepare",
            Self::BeforePublish => "before-publish",
            Self::AfterPublish => "after-publish",
            Self::BeforeCleanup => "before-cleanup",
            Self::AfterCleanup => "after-cleanup",
            Self::BeforeCommit => "before-commit",
            Self::AfterCommit => "after-commit",
            Self::BeforeBarrier => "before-barrier",
            Self::AfterBarrier => "after-barrier",
            Self::BeforeIdentity => "before-identity",
            Self::AfterIdentity => "after-identity",
            Self::BeforeSignal => "before-signal",
            Self::AfterSignal => "after-signal",
            Self::BeforeEndpoint => "before-endpoint",
            Self::AfterEndpoint => "after-endpoint",
        }
    }

    const fn fault(self) -> &'static str {
        match self {
            Self::BeforePrepare | Self::AfterPrepare => "SIGINT",
            Self::BeforePublish | Self::AfterPublish => "ROOT_EXIT",
            Self::BeforeCleanup | Self::AfterCleanup => "CLOSE_RANGE",
            Self::BeforeCommit | Self::AfterCommit => "SIGINT",
            Self::BeforeBarrier | Self::AfterBarrier => "LATE_FORK",
            Self::BeforeIdentity | Self::AfterIdentity => "PID_REUSE",
            Self::BeforeSignal | Self::AfterSignal => "TIMEOUT",
            Self::BeforeEndpoint | Self::AfterEndpoint => "LEASE_HOLDER",
        }
    }

    const fn phase(self) -> &'static str {
        match self {
            Self::BeforePrepare | Self::AfterPrepare => "PREPARE",
            Self::BeforePublish | Self::AfterPublish => "PUBLISH",
            Self::BeforeCleanup | Self::AfterCleanup => "CLEANUP",
            Self::BeforeCommit | Self::AfterCommit => "COMMIT",
            Self::BeforeBarrier | Self::AfterBarrier => "BARRIER",
            Self::BeforeIdentity | Self::AfterIdentity => "BARRIER",
            Self::BeforeSignal | Self::AfterSignal => "SIGNAL",
            Self::BeforeEndpoint | Self::AfterEndpoint => "CLEANUP",
        }
    }

    const fn execution(self) -> &'static str {
        if matches!(
            self,
            Self::BeforePrepare
                | Self::AfterPrepare
                | Self::BeforePublish
                | Self::AfterPublish
                | Self::BeforeCleanup
                | Self::AfterCleanup
                | Self::BeforeCommit
                | Self::AfterCommit
        ) {
            "REAL_OPERATION_CHECKPOINT"
        } else {
            "REDUCER_ONLY_ALIAS"
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
pub enum Interleaving {
    None,
    OrphanCgroup,
    PostDeadlineSignal,
    InodeAba,
    PartialPublication,
    Restart,
    CleanupFailure,
    LateFork,
    RootExit,
    PidReuse,
    ForkChurn,
    ConcurrentClose,
}

pub const REPRESENTATIVE_INTERLEAVINGS: [Interleaving; 12] = [
    Interleaving::None,
    Interleaving::OrphanCgroup,
    Interleaving::PostDeadlineSignal,
    Interleaving::InodeAba,
    Interleaving::PartialPublication,
    Interleaving::Restart,
    Interleaving::CleanupFailure,
    Interleaving::LateFork,
    Interleaving::RootExit,
    Interleaving::PidReuse,
    Interleaving::ForkChurn,
    Interleaving::ConcurrentClose,
];

/// Only these hooks currently execute a real namespace operation against the
/// fd-relative Boundary. The remaining names stay available to reducer-only
/// fixtures until a typed process/session seam exists.
pub const REAL_INTERLEAVINGS: [Interleaving; 4] = [
    Interleaving::None,
    Interleaving::InodeAba,
    Interleaving::PartialPublication,
    Interleaving::CleanupFailure,
];

/// Reducer-only fixtures have no typed process/session seam in the operation
/// boundary yet, but they are still executed through the authoritative
/// reducer rather than merely listed in the report.
pub const REDUCER_ONLY_INTERLEAVINGS: [Interleaving; 8] = [
    Interleaving::OrphanCgroup,
    Interleaving::PostDeadlineSignal,
    Interleaving::Restart,
    Interleaving::LateFork,
    Interleaving::RootExit,
    Interleaving::PidReuse,
    Interleaving::ForkChurn,
    Interleaving::ConcurrentClose,
];

pub const OPERATION_CHECKPOINTS: [Checkpoint; 9] = [
    Checkpoint::AfterMkdirBeforeBind,
    Checkpoint::AfterBindBeforePublish,
    Checkpoint::AfterStageMkdirBeforeBind,
    Checkpoint::AfterStageBindBeforeMove,
    Checkpoint::AfterMoveBeforeVerify,
    Checkpoint::AfterQuarantineMoveBeforeVerify,
    Checkpoint::AfterQuarantineVerifyBeforeGc,
    Checkpoint::BeforeExactMutation,
    Checkpoint::AfterExactMutation,
];

/// Recovery is reported independently from fault detection.  A fault can be
/// observed safely even when rollback has to stop and hand a retained object
/// to an external lifecycle owner.
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RecoveryStatus {
    Clean,
    ForensicRequired,
    Unsafe,
    Undetected,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct IdentityKey {
    pub device: u64,
    pub inode: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProbeOperation {
    MkdirChild,
    UnlinkExact,
    RenameExact,
}

impl ProbeOperation {
    const fn as_str(self) -> &'static str {
        match self {
            Self::MkdirChild => "MKDIR_CHILD",
            Self::UnlinkExact => "UNLINK_EXACT",
            Self::RenameExact => "RENAME_EXACT",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ProbePlacement {
    Before,
    After,
}

impl ProbePlacement {
    const fn as_str(self) -> &'static str {
        match self {
            Self::Before => "BEFORE",
            Self::After => "AFTER",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ProbeSpec {
    operation: ProbeOperation,
    checkpoint: Checkpoint,
    placement: ProbePlacement,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct RealProbe {
    operation: ProbeOperation,
    checkpoint: Checkpoint,
    placement: ProbePlacement,
    result: &'static str,
    fault_detected: bool,
    recovery_status: RecoveryStatus,
    safety_preserved: bool,
    expected_identity: Option<IdentityKey>,
    staged_identity: Option<IdentityKey>,
    obligation_identity: Option<IdentityKey>,
    real_checkpoint_observed: bool,
    real_injection_observed: bool,
    operation_outcome: &'static str,
    mutation_state: &'static str,
    interleaving_applied: bool,
    stage_obligations: usize,
    quarantine_obligations: usize,
    stage_obligation_ids: Vec<u64>,
    quarantine_obligation_ids: Vec<u64>,
    filesystem_clean: bool,
    filesystem_postcondition: bool,
    root_preserved: bool,
    forensic_root: Option<String>,
    recovery_completed: bool,
}

impl FailureBoundary {
    const fn probe_spec(self) -> ProbeSpec {
        use Checkpoint::*;
        use FailureBoundary::*;
        match self {
            BeforePrepare => ProbeSpec {
                operation: ProbeOperation::MkdirChild,
                checkpoint: AfterStageMkdirBeforeBind,
                placement: ProbePlacement::Before,
            },
            AfterPrepare => ProbeSpec {
                operation: ProbeOperation::MkdirChild,
                checkpoint: AfterMkdirBeforeBind,
                placement: ProbePlacement::After,
            },
            BeforePublish => ProbeSpec {
                operation: ProbeOperation::MkdirChild,
                checkpoint: AfterMkdirBeforeBind,
                placement: ProbePlacement::Before,
            },
            AfterPublish => ProbeSpec {
                operation: ProbeOperation::MkdirChild,
                checkpoint: AfterBindBeforePublish,
                placement: ProbePlacement::After,
            },
            BeforeCleanup => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: AfterStageBindBeforeMove,
                placement: ProbePlacement::Before,
            },
            AfterCleanup => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: AfterMoveBeforeVerify,
                placement: ProbePlacement::After,
            },
            BeforeCommit => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: BeforeExactMutation,
                placement: ProbePlacement::Before,
            },
            AfterCommit => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: AfterExactMutation,
                placement: ProbePlacement::After,
            },
            BeforeBarrier => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: AfterQuarantineMoveBeforeVerify,
                placement: ProbePlacement::Before,
            },
            AfterBarrier => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: AfterQuarantineVerifyBeforeGc,
                placement: ProbePlacement::After,
            },
            BeforeIdentity => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: BeforeExactMutation,
                placement: ProbePlacement::Before,
            },
            AfterIdentity => ProbeSpec {
                operation: ProbeOperation::UnlinkExact,
                checkpoint: AfterMoveBeforeVerify,
                placement: ProbePlacement::After,
            },
            BeforeSignal => ProbeSpec {
                operation: ProbeOperation::RenameExact,
                checkpoint: AfterMoveBeforeVerify,
                placement: ProbePlacement::Before,
            },
            AfterSignal => ProbeSpec {
                operation: ProbeOperation::RenameExact,
                checkpoint: AfterExactMutation,
                placement: ProbePlacement::After,
            },
            BeforeEndpoint => ProbeSpec {
                operation: ProbeOperation::RenameExact,
                checkpoint: BeforeExactMutation,
                placement: ProbePlacement::Before,
            },
            AfterEndpoint => ProbeSpec {
                operation: ProbeOperation::RenameExact,
                checkpoint: AfterExactMutation,
                placement: ProbePlacement::After,
            },
        }
    }
}

impl Interleaving {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::None => "none",
            Self::OrphanCgroup => "orphan-cgroup",
            Self::PostDeadlineSignal => "post-deadline-signal",
            Self::InodeAba => "inode-aba",
            Self::PartialPublication => "partial-publication",
            Self::Restart => "restart",
            Self::CleanupFailure => "cleanup-failure",
            Self::LateFork => "late-fork",
            Self::RootExit => "root-exit",
            Self::PidReuse => "pid-reuse",
            Self::ForkChurn => "fork-churn",
            Self::ConcurrentClose => "concurrent-close",
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ExplorerBudget {
    pub max_scenarios: usize,
    pub max_events: usize,
    pub max_virtual_time_ns: u64,
    pub max_live_capabilities: usize,
    pub max_recovery_steps: usize,
    pub max_schedule_steps: usize,
}

impl Default for ExplorerBudget {
    fn default() -> Self {
        Self {
            max_scenarios: FAILURE_BOUNDARIES.len() * REAL_INTERLEAVINGS.len()
                + REDUCER_ONLY_FAILURE_BOUNDARIES.len() * REDUCER_ONLY_INTERLEAVINGS.len(),
            max_events: 32,
            max_virtual_time_ns: 100_000,
            max_live_capabilities: 16,
            max_recovery_steps: 8,
            max_schedule_steps: 8,
        }
    }
}

impl ExplorerBudget {
    pub fn validate(&self) -> ExplorerResult<()> {
        if self.max_scenarios == 0
            || self.max_scenarios
                > FAILURE_BOUNDARIES.len() * REAL_INTERLEAVINGS.len()
                    + REDUCER_ONLY_FAILURE_BOUNDARIES.len() * REDUCER_ONLY_INTERLEAVINGS.len()
        {
            return Err(ExplorerError::Budget("scenario matrix"));
        }
        if self.max_events == 0 || self.max_events > MODEL_MAX_EVENTS {
            return Err(ExplorerError::Budget("events"));
        }
        if self.max_virtual_time_ns == 0 || self.max_virtual_time_ns > MODEL_MAX_VIRTUAL_TIME_NS {
            return Err(ExplorerError::Budget("virtual time"));
        }
        if self.max_live_capabilities == 0
            || self.max_live_capabilities > MODEL_MAX_LIVE_CAPABILITIES
        {
            return Err(ExplorerError::Budget("live capabilities"));
        }
        if self.max_recovery_steps == 0 || self.max_recovery_steps > MODEL_MAX_RECOVERY_STEPS {
            return Err(ExplorerError::Budget("recovery steps"));
        }
        if self.max_schedule_steps == 0 || self.max_schedule_steps > self.max_events {
            return Err(ExplorerError::Budget("schedule steps"));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum ExplorerError {
    Budget(&'static str),
    Reducer(String),
    Invariant(String),
    Undetected(String),
}

impl fmt::Display for ExplorerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Budget(name) => write!(formatter, "explorer budget exceeded: {name}"),
            Self::Reducer(error) => write!(formatter, "reducer rejected explorer step: {error}"),
            Self::Invariant(error) => write!(formatter, "explorer invariant failure: {error}"),
            Self::Undetected(scenario) => {
                write!(formatter, "scenario was not detected: {scenario}")
            }
        }
    }
}

impl std::error::Error for ExplorerError {}

pub type ExplorerResult<T> = std::result::Result<T, ExplorerError>;

#[derive(Clone, Debug)]
pub struct VirtualClock {
    now_ns: u64,
    max_ns: u64,
}

impl VirtualClock {
    pub fn new(max_ns: u64) -> ExplorerResult<Self> {
        if max_ns == 0 {
            return Err(ExplorerError::Budget("virtual time"));
        }
        Ok(Self { now_ns: 0, max_ns })
    }

    pub fn now_ns(&self) -> u64 {
        self.now_ns
    }

    pub fn advance(&mut self, delta_ns: u64) -> ExplorerResult<()> {
        let next = self
            .now_ns
            .checked_add(delta_ns)
            .ok_or(ExplorerError::Budget("virtual time overflow"))?;
        if next > self.max_ns {
            return Err(ExplorerError::Budget("virtual time"));
        }
        self.now_ns = next;
        Ok(())
    }
}

impl Clock for VirtualClock {
    fn now_ns(&mut self) -> u64 {
        self.now_ns
    }

    fn sleep_ns(&mut self, duration_ns: u64) {
        self.now_ns = self.now_ns.saturating_add(duration_ns).min(self.max_ns);
    }
}

#[derive(Clone, Debug)]
struct CatalogSpec {
    id: &'static str,
    kind: CapabilityKind,
    token: &'static str,
}

#[derive(Clone, Debug)]
struct DraftEvent {
    actor: &'static str,
    event: Event,
    data: Value,
    delta_ns: u64,
}

#[derive(Clone, Debug)]
struct Draft {
    scenario: String,
    initial_stable: StableState,
    catalog: Vec<CatalogSpec>,
    events: Vec<DraftEvent>,
    schedule_steps: usize,
    real_probe: RealProbe,
}

#[derive(Clone, Debug, Serialize)]
pub struct ScenarioResult {
    pub scenario: String,
    pub seed: u64,
    pub failure_boundary: String,
    pub boundary_execution: String,
    pub interleaving: String,
    pub fault_detected: bool,
    pub recovery_status: RecoveryStatus,
    pub safety_preserved: bool,
    pub diagnostic_error: Option<String>,
    pub outcome: String,
    pub event_count: usize,
    pub minimized_event_count: usize,
    pub schedule_steps: usize,
    pub operation: String,
    pub checkpoint: String,
    pub placement: String,
    pub probe_result: String,
    pub real_checkpoint_observed: bool,
    pub real_injection_observed: bool,
    pub operation_outcome: String,
    pub mutation_state: String,
    pub interleaving_applied: bool,
    pub stage_obligations: usize,
    pub quarantine_obligations: usize,
    pub stage_obligation_ids: Vec<u64>,
    pub quarantine_obligation_ids: Vec<u64>,
    pub filesystem_clean: bool,
    pub filesystem_postcondition: bool,
    pub root_preserved: bool,
    pub forensic_root: Option<String>,
    pub recovery_completed: bool,
    pub expected_identity: Option<IdentityKey>,
    pub staged_identity: Option<IdentityKey>,
    pub obligation_identity: Option<IdentityKey>,
    pub typed_rejection_observed: bool,
    pub typed_recovery_observed: bool,
    pub unresolved_obligations: Vec<String>,
    pub virtual_time_ns: u64,
    pub invariant_complete: bool,
    pub trace: Value,
}

#[derive(Clone, Debug, Serialize)]
pub struct ExplorerReport {
    pub explorer_version: u32,
    pub seed: u64,
    pub budget: ExplorerBudget,
    pub declared_failure_boundaries: Vec<String>,
    pub real_failure_boundaries: Vec<String>,
    pub reducer_only_failure_boundaries: Vec<String>,
    pub representative_interleavings: Vec<String>,
    pub reducer_only_interleavings: Vec<String>,
    pub declared_operation_checkpoints: Vec<String>,
    pub scenario_count: usize,
    pub real_scenario_count: usize,
    pub reducer_only_alias_scenario_count: usize,
    pub reducer_only_scenario_count: usize,
    pub clean_count: usize,
    pub forensic_count: usize,
    pub unsafe_count: usize,
    pub undetected_count: usize,
    pub base_matrix_clean_count: usize,
    pub base_matrix_forensic_count: usize,
    pub reducer_only_matrix_clean_count: usize,
    pub reducer_only_matrix_forensic_count: usize,
    pub scenarios: Vec<ScenarioResult>,
}

#[derive(Clone, Debug)]
struct Run {
    reducer: Reducer,
    snapshots: Vec<StateSnapshot>,
    times: Vec<u64>,
}

struct ProbeFault {
    checkpoint: Checkpoint,
    placement: ProbePlacement,
    fired: bool,
    root: PathBuf,
    operation: ProbeOperation,
    interleaving: Interleaving,
    hook_applied: Arc<AtomicBool>,
    scope_invalidated: Arc<AtomicBool>,
}

impl FaultInjector for ProbeFault {
    fn invalidates_quiescence(&self) -> bool {
        self.scope_invalidated.load(Ordering::SeqCst)
    }

    fn take_quiescence_invalidation(&mut self) -> bool {
        self.scope_invalidated.swap(false, Ordering::SeqCst)
    }

    fn checkpoint(&mut self, checkpoint: Checkpoint) -> crate::Result<()> {
        if self.placement == ProbePlacement::Before {
            return self.inject_if_selected(checkpoint);
        }
        Ok(())
    }

    fn after_checkpoint(&mut self, checkpoint: Checkpoint) -> crate::Result<()> {
        if self.placement == ProbePlacement::After {
            return self.inject_if_selected(checkpoint);
        }
        Ok(())
    }
}

impl ProbeFault {
    fn inject_if_selected(&mut self, checkpoint: Checkpoint) -> crate::Result<()> {
        if checkpoint == self.checkpoint && !self.fired {
            // Boundary::checkpoint temporarily withdraws the parked
            // quiescence scope while invoking this hook, so both BEFORE and
            // AFTER namespace interleavings execute outside the final
            // mutation authority.
            if self.interleaving != Interleaving::None {
                // Mark the external proof invalid before running the hook:
                // even a hook that fails half-way may already have changed
                // the namespace and the parked scope must never be restored.
                self.scope_invalidated.store(true, Ordering::SeqCst);
                apply_interleaving_hook(&self.root, self.operation, self.interleaving, checkpoint)?;
                self.hook_applied.store(true, Ordering::SeqCst);
            }
            self.fired = true;
            return Err(BoundaryError::Injected(checkpoint));
        }
        Ok(())
    }
}

fn probe_io(operation: &'static str, source: std::io::Error) -> BoundaryError {
    BoundaryError::Io { operation, source }
}

fn marker_path(root: &Path, interleaving: Interleaving) -> PathBuf {
    root.join(format!(".explorer-interleaving-{}", interleaving.as_str()))
}

fn fork_and_reap() -> crate::Result<()> {
    // SAFETY: the child exits immediately and the parent waits for this
    // specific PID.  No inherited Rust state is used after fork.
    let child = unsafe { libc::fork() };
    if child < 0 {
        return Err(probe_io("explorer fork", std::io::Error::last_os_error()));
    }
    if child == 0 {
        unsafe { libc::_exit(0) };
    }
    let mut status = 0;
    // SAFETY: `child` is the PID returned by fork and `status` is writable.
    if unsafe { libc::waitpid(child, &mut status, 0) } < 0 {
        return Err(probe_io(
            "explorer waitpid",
            std::io::Error::last_os_error(),
        ));
    }
    Ok(())
}

fn apply_interleaving_hook(
    root: &Path,
    operation: ProbeOperation,
    interleaving: Interleaving,
    checkpoint: Checkpoint,
) -> crate::Result<()> {
    fs::write(
        marker_path(root, interleaving),
        format!("{}@{}", operation.as_str(), interleaving.as_str()),
    )
    .map_err(|error| probe_io("explorer interleaving marker", error))?;
    match interleaving {
        Interleaving::None => {}
        Interleaving::InodeAba | Interleaving::PidReuse => {
            let name = match operation {
                ProbeOperation::MkdirChild => "created",
                ProbeOperation::UnlinkExact => "target",
                ProbeOperation::RenameExact => "source",
            };
            let path = root.join(name);
            let after_move = matches!(
                checkpoint,
                Checkpoint::AfterMoveBeforeVerify
                    | Checkpoint::AfterQuarantineMoveBeforeVerify
                    | Checkpoint::AfterQuarantineVerifyBeforeGc
                    | Checkpoint::AfterExactMutation
            );
            if operation == ProbeOperation::MkdirChild || after_move {
                if operation == ProbeOperation::MkdirChild {
                    fs::create_dir(&path)
                        .map_err(|error| probe_io("explorer ABA directory", error))?;
                } else {
                    fs::write(&path, b"replacement")
                        .map_err(|error| probe_io("explorer ABA replacement", error))?;
                }
            } else {
                let saved = root.join(format!(".explorer-original-{name}"));
                fs::rename(&path, &saved).map_err(|error| probe_io("explorer ABA move", error))?;
                fs::write(&path, b"replacement")
                    .map_err(|error| probe_io("explorer ABA replacement", error))?;
            }
            if interleaving == Interleaving::PidReuse {
                fork_and_reap()?;
            }
        }
        Interleaving::PartialPublication => {
            let blocker = match operation {
                ProbeOperation::MkdirChild => "created",
                ProbeOperation::RenameExact => "destination",
                ProbeOperation::UnlinkExact => "target",
            };
            if operation == ProbeOperation::MkdirChild {
                fs::create_dir(root.join(blocker))
                    .map_err(|error| probe_io("explorer publication blocker", error))?;
            } else {
                fs::write(root.join(blocker), b"publication-blocker")
                    .map_err(|error| probe_io("explorer publication blocker", error))?;
            }
        }
        Interleaving::ConcurrentClose => {
            // The operation operand may already be in its private stage at
            // an AFTER checkpoint; duplicate the anchored probe root instead
            // of relying on a pathname that the operation has moved.
            let file =
                fs::File::open(root).map_err(|error| probe_io("explorer close open", error))?;
            // SAFETY: the duplicated descriptor is closed immediately and
            // never aliases a capability owned by Boundary.
            let duplicate = unsafe { libc::dup(file.as_raw_fd()) };
            if duplicate < 0 {
                return Err(probe_io("explorer dup", std::io::Error::last_os_error()));
            }
            if unsafe { libc::close(duplicate) } < 0 {
                return Err(probe_io("explorer close", std::io::Error::last_os_error()));
            }
        }
        Interleaving::LateFork | Interleaving::RootExit => {
            fork_and_reap()?;
        }
        Interleaving::ForkChurn => {
            for _ in 0..3 {
                fork_and_reap()?;
            }
        }
        Interleaving::CleanupFailure => match operation {
            ProbeOperation::MkdirChild => {
                fs::create_dir(root.join("created"))
                    .map_err(|error| probe_io("explorer cleanup blocker", error))?;
            }
            ProbeOperation::UnlinkExact | ProbeOperation::RenameExact => {
                let name = if operation == ProbeOperation::UnlinkExact {
                    "target"
                } else {
                    "source"
                };
                let path = root.join(name);
                if matches!(
                    checkpoint,
                    Checkpoint::AfterMoveBeforeVerify
                        | Checkpoint::AfterQuarantineMoveBeforeVerify
                        | Checkpoint::AfterQuarantineVerifyBeforeGc
                        | Checkpoint::AfterExactMutation
                ) {
                    fs::write(&path, b"cleanup-replacement")
                        .map_err(|error| probe_io("explorer cleanup replacement", error))?;
                } else {
                    let saved = root.join(format!(".explorer-original-{name}"));
                    fs::rename(&path, &saved)
                        .map_err(|error| probe_io("explorer cleanup ABA move", error))?;
                    fs::write(&path, b"cleanup-replacement")
                        .map_err(|error| probe_io("explorer cleanup replacement", error))?;
                }
            }
        },
        Interleaving::OrphanCgroup | Interleaving::PostDeadlineSignal | Interleaving::Restart => {
            fork_and_reap()?;
        }
    }
    Ok(())
}

fn probe_root(seed: u64) -> ExplorerResult<PathBuf> {
    let mut path = std::env::temp_dir();
    let serial = NEXT_PROBE_ROOT.fetch_add(1, Ordering::Relaxed);
    path.push(format!(
        "darling-lifecycle-explorer-{}-{seed}-{serial}",
        std::process::id(),
    ));
    fs::create_dir(&path).map_err(|error| ExplorerError::Reducer(error.to_string()))?;
    fs::write(path.join("lock"), b"").map_err(|error| ExplorerError::Reducer(error.to_string()))?;
    fs::write(path.join("target"), b"target")
        .map_err(|error| ExplorerError::Reducer(error.to_string()))?;
    fs::write(path.join("source"), b"source")
        .map_err(|error| ExplorerError::Reducer(error.to_string()))?;
    Ok(path)
}

fn operation_name(operation: ProbeOperation) -> OperationName {
    match operation {
        ProbeOperation::MkdirChild => OperationName::MkdirChild,
        ProbeOperation::UnlinkExact => OperationName::UnlinkExact,
        ProbeOperation::RenameExact => OperationName::RenameExact,
    }
}

fn operation_outcome_name(outcome: OperationOutcome) -> &'static str {
    match outcome {
        OperationOutcome::Observed => "OBSERVED",
        OperationOutcome::Applied => "APPLIED",
        OperationOutcome::Error => "ERROR",
        OperationOutcome::Injected => "INJECTED",
        OperationOutcome::Allocated => "ALLOCATED",
        OperationOutcome::Registered => "REGISTERED",
        OperationOutcome::Published => "PUBLISHED",
        OperationOutcome::Deferred => "DEFERRED",
        OperationOutcome::StagedError => "STAGED_ERROR",
        OperationOutcome::RolledBackError => "ROLLED_BACK_ERROR",
        OperationOutcome::RollbackIncomplete => "ROLLBACK_INCOMPLETE",
        OperationOutcome::IdentityMismatch => "IDENTITY_MISMATCH",
        OperationOutcome::MutatedError => "MUTATED_ERROR",
    }
}

fn mutation_state_name(state: MutationState) -> &'static str {
    match state {
        MutationState::NotAttempted => "NOT_ATTEMPTED",
        MutationState::Applied => "APPLIED",
        MutationState::Partial => "PARTIAL",
        MutationState::RolledBack => "ROLLED_BACK",
        MutationState::RollbackIncomplete => "ROLLBACK_INCOMPLETE",
    }
}

fn cleanup_interleaving_artifacts(
    root: &Path,
    operation: ProbeOperation,
    interleaving: Interleaving,
) {
    fn remove_any(path: PathBuf) {
        if fs::remove_file(&path).is_err() {
            let _ = fs::remove_dir_all(path);
        }
    }
    remove_any(marker_path(root, interleaving));
    remove_any(root.join(".explorer-unlink-blocker"));
    let name = match operation {
        ProbeOperation::MkdirChild | ProbeOperation::UnlinkExact => "target",
        ProbeOperation::RenameExact => "source",
    };
    remove_any(root.join(format!(".explorer-original-{name}")));
}

fn filesystem_clean(root: &Path, _operation: ProbeOperation) -> bool {
    let Ok(entries) = fs::read_dir(root) else {
        return false;
    };
    entries.flatten().all(|entry| {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(".lifecycle-") || name.starts_with(".explorer-") {
            return false;
        }
        true
    })
}

fn regular_file_contains(path: PathBuf, expected: &[u8]) -> bool {
    fs::read(path).is_ok_and(|contents| contents == expected)
}

/// Check the product-visible postcondition before disposable interleaving
/// artifacts are removed.  This is deliberately derived from the observed
/// operation result and the actual directory entries, not from the reducer
/// trace or from an expected value supplied by the scenario.
fn operation_postcondition(
    root: &Path,
    operation: ProbeOperation,
    outcome: &'static str,
    _mutation: &'static str,
    interleaving: Interleaving,
) -> bool {
    // An interleaving may intentionally publish a foreign replacement or a
    // publication blocker.  That object is expected to survive the failed
    // operation; the probe teardown removes only this known fixture after
    // recording the product-visible outcome.
    let foreign_replacement = replacement_survives(root, operation);
    if interleaving != Interleaving::None && foreign_replacement {
        return true;
    }
    let mutation_applied = matches!(outcome, "APPLIED" | "MUTATED_ERROR");
    match operation {
        ProbeOperation::MkdirChild => {
            let path = root.join("created");
            if mutation_applied {
                fs::metadata(path).is_ok_and(|metadata| metadata.is_dir())
            } else {
                !path.exists()
            }
        }
        ProbeOperation::UnlinkExact => {
            let path = root.join("target");
            if mutation_applied {
                !path.exists()
            } else {
                regular_file_contains(path, b"target")
            }
        }
        ProbeOperation::RenameExact => {
            let source = root.join("source");
            let destination = root.join("destination");
            if mutation_applied {
                !source.exists() && regular_file_contains(destination, b"source")
            } else {
                regular_file_contains(source, b"source") && !destination.exists()
            }
        }
    }
}

fn replacement_survives(root: &Path, operation: ProbeOperation) -> bool {
    match operation {
        ProbeOperation::MkdirChild => fs::metadata(root.join("created")).is_ok_and(|m| m.is_dir()),
        ProbeOperation::UnlinkExact => {
            regular_file_contains(root.join("target"), b"replacement")
                || regular_file_contains(root.join("target"), b"cleanup-replacement")
                || regular_file_contains(root.join("target"), b"publication-blocker")
        }
        ProbeOperation::RenameExact => {
            regular_file_contains(root.join("source"), b"replacement")
                || regular_file_contains(root.join("source"), b"cleanup-replacement")
                || regular_file_contains(root.join("destination"), b"publication-blocker")
        }
    }
}

fn path_identity_key(path: &Path) -> Option<IdentityKey> {
    let metadata = fs::symlink_metadata(path).ok()?;
    Some(IdentityKey {
        device: metadata.dev(),
        inode: metadata.ino(),
    })
}

/// Return the identity of the retained child in the one forensic stage.  This
/// is read-only evidence; recovery never rediscovers or mutates authority by
/// pathname.  The boundary has already retained the typed stage obligation.
fn staged_entry_identity(root: &Path) -> Option<IdentityKey> {
    let stage = fs::read_dir(root)
        .ok()?
        .filter_map(std::result::Result::ok)
        .map(|entry| entry.path())
        .filter(|path| {
            path.file_name()
                .is_some_and(|name| name.to_string_lossy().starts_with(".lifecycle-stage-"))
        })
        .collect::<Vec<_>>();
    if stage.len() != 1 {
        return None;
    }
    path_identity_key(&stage[0].join("entry"))
}

struct SafetyInputs<'a> {
    root: &'a Path,
    operation: ProbeOperation,
    expected_identity: Option<IdentityKey>,
    staged_identity: Option<IdentityKey>,
    obligation_identity: Option<IdentityKey>,
    stage_obligations: usize,
    quarantine_obligations: usize,
    filesystem_clean: bool,
    filesystem_postcondition: bool,
}

fn safety_preserved(inputs: SafetyInputs<'_>) -> bool {
    if !inputs.filesystem_postcondition || inputs.quarantine_obligations != 0 {
        return false;
    }
    if inputs.stage_obligations == 0 {
        return inputs.filesystem_clean;
    }
    // The only retained-stage cases currently admitted by the explorer are
    // explicit forensic outcomes.  The expected inode and the foreign
    // replacement must both remain present; no forward-finalization is
    // inferred from the operation name.
    inputs.stage_obligations == 1
        && matches!(
            inputs.operation,
            ProbeOperation::UnlinkExact | ProbeOperation::RenameExact
        )
        && inputs.expected_identity == inputs.staged_identity
        && inputs.expected_identity == inputs.obligation_identity
        && inputs.expected_identity.is_some()
        && replacement_survives(inputs.root, inputs.operation)
}

/// Execute the recovery obligations emitted by the real Boundary operation.
///
/// The explorer is the external lifecycle controller for its disposable
/// namespace, so it can establish the writer-stop proof and use the retained
/// parent capabilities carried by each obligation.  No pathname is
/// rediscovered: bound quarantine/stage records retain their parent FD and
/// unbound records are recovered only when they retained one and an expected
/// identity.  On any failure the unprocessed ownership records are put back
/// into the boundary and the caller must retain the forensic root.
fn drain_recovery_queue<T, E, F>(
    items: Vec<T>,
    mut recover: F,
) -> std::result::Result<(), Box<(E, Vec<T>)>>
where
    F: FnMut(T) -> std::result::Result<(), Box<(T, E)>>,
{
    let mut iterator = items.into_iter();
    while let Some(item) = iterator.next() {
        match recover(item) {
            Ok(()) => {}
            Err(error) => {
                let (item, error) = *error;
                let mut remaining = vec![item];
                remaining.extend(iterator);
                return Err(Box::new((error, remaining)));
            }
        }
    }
    Ok(())
}

fn recover_boundary_obligations<C: Clock, O: Observer, F: FaultInjector>(
    boundary: &mut Boundary<C, O, F>,
    lease: &ExclusiveLease,
) -> crate::Result<()> {
    let quarantines = std::mem::take(&mut boundary.pending_quarantines);
    let quarantine_result = drain_recovery_queue(quarantines, |obligation| match obligation {
        QuarantineObligation::Bound(object) => {
            let parent = object.parent_cap();
            // SAFETY: the probe has no namespace writers after the
            // single-threaded operation returns; this is the explicit
            // external quiescence handoff required by the boundary.
            let scope = match unsafe { QuiescentScope::from_external(parent, lease) } {
                Ok(scope) => scope,
                Err(error) => {
                    return Err(Box::new((QuarantineObligation::Bound(object), error)));
                }
            };
            if let Err(error) = boundary.gc_quarantine(parent, lease, &scope, &object) {
                return Err(Box::new((QuarantineObligation::Bound(object), error)));
            }
            Ok(())
        }
        QuarantineObligation::Unbound(unbound) => {
            let Some(parent_fd) = unbound.parent_fd() else {
                return Err(Box::new((
                    QuarantineObligation::Unbound(unbound),
                    BoundaryError::QuarantineRequired,
                )));
            };
            let expected = unbound.expected;
            let parent_fd = match duplicate_fd(parent_fd) {
                Ok(fd) => fd,
                Err(error) => {
                    return Err(Box::new((QuarantineObligation::Unbound(unbound), error)));
                }
            };
            let parent = DirCap::new(parent_fd, unbound.parent_identity, 0, unbound.scope);
            let observed = match FileIdentity::from_at(parent.raw_fd(), &unbound.quarantine_name) {
                Ok(identity) => identity,
                Err(error) => {
                    return Err(Box::new((QuarantineObligation::Unbound(unbound), error)));
                }
            };
            if observed.inode_key() != expected.inode_key() {
                return Err(Box::new((
                    QuarantineObligation::Unbound(unbound),
                    BoundaryError::IdentityMismatch,
                )));
            }
            if let Err(error) =
                unlink_at(parent.raw_fd(), &unbound.quarantine_name, unbound.directory)
            {
                return Err(Box::new((QuarantineObligation::Unbound(unbound), error)));
            }
            Ok(())
        }
    });
    if let Err(error) = quarantine_result {
        let (error, remaining) = *error;
        // `drain_recovery_queue` returns the failed item followed by the
        // untouched tail, preserving both ownership and original ordering.
        boundary.pending_quarantines.extend(remaining);
        return Err(error);
    }

    let stages = std::mem::take(&mut boundary.pending_stages);
    let stage_result = drain_recovery_queue(stages, |obligation| match obligation {
        StageObligation::Bound(stage) => {
            if stage.parent.scope() != lease.scope() {
                return Err(Box::new((
                    StageObligation::Bound(stage),
                    BoundaryError::WrongCapability("stage recovery"),
                )));
            }
            let observed = match FileIdentity::from_at(stage.parent.raw_fd(), &stage.name) {
                Ok(identity) => identity,
                Err(error) => {
                    return Err(Box::new((StageObligation::Bound(stage), error)));
                }
            };
            if observed.inode_key() != stage.stage.identity().inode_key() {
                return Err(Box::new((
                    StageObligation::Bound(stage),
                    BoundaryError::IdentityMismatch,
                )));
            }
            if let Err(error) = unlink_at(stage.parent.raw_fd(), &stage.name, true) {
                return Err(Box::new((StageObligation::Bound(stage), error)));
            }
            Ok(())
        }
        StageObligation::Unbound(stage) => {
            let Some(parent_fd) = stage.parent_fd.as_ref() else {
                return Err(Box::new((
                    StageObligation::Unbound(stage),
                    BoundaryError::WrongCapability("stage recovery"),
                )));
            };
            let Some(expected) = stage.expected else {
                return Err(Box::new((
                    StageObligation::Unbound(stage),
                    BoundaryError::WrongCapability("stage recovery"),
                )));
            };
            let parent_fd = match duplicate_fd(parent_fd.as_raw_fd()) {
                Ok(fd) => fd,
                Err(error) => {
                    return Err(Box::new((StageObligation::Unbound(stage), error)));
                }
            };
            let parent = DirCap::new(parent_fd, stage.parent_identity, 0, stage.scope);
            let observed = match FileIdentity::from_at(parent.raw_fd(), &stage.name) {
                Ok(identity) => identity,
                Err(error) => {
                    return Err(Box::new((StageObligation::Unbound(stage), error)));
                }
            };
            if observed.inode_key() != expected.inode_key() {
                return Err(Box::new((
                    StageObligation::Unbound(stage),
                    BoundaryError::IdentityMismatch,
                )));
            }
            if let Err(error) = unlink_at(parent.raw_fd(), &stage.name, true) {
                return Err(Box::new((StageObligation::Unbound(stage), error)));
            }
            Ok(())
        }
    });
    if let Err(error) = stage_result {
        let (error, remaining) = *error;
        boundary.pending_stages.extend(remaining);
        return Err(error);
    }
    Ok(())
}

fn run_real_probe(
    spec: ProbeSpec,
    interleaving: Interleaving,
    seed: u64,
) -> ExplorerResult<RealProbe> {
    let root = probe_root(seed)?;
    let hook_applied = Arc::new(AtomicBool::new(interleaving == Interleaving::None));
    let scope_invalidated = Arc::new(AtomicBool::new(false));
    let fault = ProbeFault {
        checkpoint: spec.checkpoint,
        placement: spec.placement,
        fired: false,
        root: root.clone(),
        operation: spec.operation,
        interleaving,
        hook_applied: hook_applied.clone(),
        scope_invalidated,
    };
    let mut boundary = Boundary::new(
        VirtualClock::new(MODEL_MAX_VIRTUAL_TIME_NS)
            .map_err(|error| ExplorerError::Reducer(error.to_string()))?,
        VecObserver::default(),
        fault,
    );
    let mut recovery_completed = false;
    let mut expected_identity = None;
    let operation_result = (|| -> crate::Result<()> {
        let parent = boundary.anchor_directory(&root)?;
        let lock = boundary.open_lock(&parent, "lock")?;
        let lease = boundary.flock_exclusive(lock, MODEL_MAX_VIRTUAL_TIME_NS)?;
        let expected = match spec.operation {
            ProbeOperation::MkdirChild => None,
            ProbeOperation::UnlinkExact => {
                Some(boundary.open_file(&parent, "target", false)?.identity())
            }
            ProbeOperation::RenameExact => {
                Some(boundary.open_file(&parent, "source", false)?.identity())
            }
        };
        expected_identity = expected.map(|identity| IdentityKey {
            device: identity.device,
            inode: identity.inode,
        });
        // Final exact mutations require an external writer-stop proof. A
        // non-cooperative BEFORE hook runs before that proof is handed to the
        // boundary; AFTER hooks are invoked with the parked proof temporarily
        // withdrawn by Boundary::checkpoint.
        let scope_before_operation =
            interleaving == Interleaving::None || spec.placement == ProbePlacement::After;
        if scope_before_operation {
            // SAFETY: `None` has no writer hook, and AFTER probes park the
            // proof while their hook runs. The disposable probe has no other
            // namespace writers at this point.
            let scope = unsafe { QuiescentScope::from_external(&parent, &lease)? };
            boundary.set_quiescent_scope(scope);
        }
        // The interleaving hook is invoked by the configured FaultInjector at
        // the selected real checkpoint while these anchored capabilities are
        // still live.  It is therefore part of the operation path rather
        // than a post-operation synthetic event.
        let operation_result = match spec.operation {
            ProbeOperation::MkdirChild => boundary
                .mkdir_child(&parent, &lease, "created", 0o700)
                .map(|child| boundary.close_dir(child)),
            ProbeOperation::UnlinkExact => boundary.unlink_exact(
                &parent,
                &lease,
                "target",
                expected.expect("unlink probe identity"),
                false,
            ),
            ProbeOperation::RenameExact => boundary.rename_exact(
                &parent,
                &parent,
                &lease,
                "source",
                "destination",
                expected.expect("rename probe identity"),
            ),
        };
        // Any writer hook irreversibly invalidates the old proof.  Establish a
        // fresh post-operation barrier for recovery (including NONE/AFTER);
        // never resurrect the scope that existed before the hook.
        // SAFETY: the probe is single-threaded after the operation returns,
        // so this is the explicit external quiescence handoff for recovery.
        let scope = unsafe { QuiescentScope::from_external(&parent, &lease)? };
        boundary.set_quiescent_scope(scope);
        recovery_completed = recover_boundary_obligations(&mut boundary, &lease).is_ok();
        operation_result
    })();
    // Recovery is a real second phase of the probe, not a reducer-only
    // observation.  It consumes every retained obligation when possible and
    // leaves failures in the boundary queues for forensic reporting.
    let parts = boundary.into_parts();
    let saw_checkpoint = parts
        .observer
        .records
        .iter()
        .any(|record| record.checkpoint() == Some(spec.checkpoint));
    let injected = parts.observer.records.iter().any(|record| {
        record.checkpoint() == Some(spec.checkpoint)
            && record.outcome() == crate::OperationOutcome::Injected
    });
    let operation_record = parts
        .observer
        .records
        .iter()
        .rev()
        .find(|record| record.operation() == operation_name(spec.operation));
    let operation_outcome = operation_record
        .map(|record| operation_outcome_name(record.outcome()))
        .unwrap_or("ERROR");
    let mutation_state = operation_record
        .map(|record| mutation_state_name(record.mutation()))
        .unwrap_or("NOT_ATTEMPTED");
    // The checkpoint event is authoritative for fault placement.  A real
    // operation may then return a rollback/identity error caused by the
    // interleaving; requiring the outer return value to remain Injected would
    // discard precisely that post-mutation evidence.
    let result = if injected {
        "INJECTED"
    } else if operation_result.is_ok() {
        "COMPLETED"
    } else {
        "ERROR"
    };
    let stage_obligations = parts.stage_obligations.len();
    let quarantine_obligations = parts.quarantine_obligations.len();
    let stage_obligation_ids = parts.stage_obligations.ids();
    let obligation_identity = parts
        .stage_obligations
        .child_identities()
        .first()
        .copied()
        .flatten()
        .map(|identity| IdentityKey {
            device: identity.device,
            inode: identity.inode,
        });
    let quarantine_obligation_ids = parts.quarantine_obligations.ids();
    let filesystem_postcondition = operation_postcondition(
        &root,
        spec.operation,
        operation_outcome,
        mutation_state,
        interleaving,
    );
    let staged_identity = staged_entry_identity(&root);
    // Never mutate a root that contains an unresolved obligation or a failed
    // product postcondition.  Those roots are forensic evidence and the
    // exact path is returned to the caller; cleanup is only allowed after the
    // real boundary has reached a clean terminal state.
    let retain_before_cleanup =
        stage_obligations != 0 || quarantine_obligations != 0 || !filesystem_postcondition;
    if !retain_before_cleanup {
        cleanup_interleaving_artifacts(&root, spec.operation, interleaving);
    }
    let filesystem_clean = filesystem_clean(&root, spec.operation);
    let safety_preserved = safety_preserved(SafetyInputs {
        root: &root,
        operation: spec.operation,
        expected_identity,
        staged_identity,
        obligation_identity,
        stage_obligations,
        quarantine_obligations,
        filesystem_clean,
        filesystem_postcondition,
    });
    let root_preserved = retain_before_cleanup || !filesystem_clean;
    let forensic_root = if root_preserved {
        Some(root.display().to_string())
    } else {
        None
    };
    if !root_preserved {
        fs::remove_dir_all(&root).map_err(|error| ExplorerError::Reducer(error.to_string()))?;
    }
    if !saw_checkpoint {
        return Err(ExplorerError::Invariant(format!(
            "real boundary checkpoint {} was not reached for {} / {}",
            checkpoint_name(spec.checkpoint),
            spec.operation.as_str(),
            interleaving.as_str(),
        )));
    }
    if result != "INJECTED" {
        return Err(ExplorerError::Invariant(format!(
            "real operation did not inject at {} for {} / {}",
            checkpoint_name(spec.checkpoint),
            spec.operation.as_str(),
            interleaving.as_str(),
        )));
    }
    let fault_detected = injected && saw_checkpoint && result == "INJECTED";
    let recovery_status = if !fault_detected {
        RecoveryStatus::Undetected
    } else if !safety_preserved {
        RecoveryStatus::Unsafe
    } else if stage_obligations == 0
        && quarantine_obligations == 0
        && filesystem_clean
        && filesystem_postcondition
        && !root_preserved
        && recovery_completed
    {
        RecoveryStatus::Clean
    } else {
        RecoveryStatus::ForensicRequired
    };
    Ok(RealProbe {
        operation: spec.operation,
        checkpoint: spec.checkpoint,
        placement: spec.placement,
        result,
        fault_detected,
        recovery_status,
        safety_preserved,
        expected_identity,
        staged_identity,
        obligation_identity,
        real_checkpoint_observed: saw_checkpoint,
        real_injection_observed: injected,
        operation_outcome,
        mutation_state,
        interleaving_applied: hook_applied.load(Ordering::SeqCst),
        stage_obligations,
        quarantine_obligations,
        stage_obligation_ids,
        quarantine_obligation_ids,
        filesystem_clean,
        filesystem_postcondition,
        root_preserved,
        forensic_root,
        recovery_completed,
    })
}

fn reducer_only_probe(boundary: FailureBoundary, _interleaving: Interleaving) -> RealProbe {
    let spec = boundary.probe_spec();
    RealProbe {
        operation: spec.operation,
        checkpoint: spec.checkpoint,
        placement: spec.placement,
        result: "INJECTED",
        fault_detected: true,
        recovery_status: RecoveryStatus::Clean,
        safety_preserved: true,
        expected_identity: None,
        staged_identity: None,
        obligation_identity: None,
        real_checkpoint_observed: false,
        real_injection_observed: false,
        operation_outcome: "ERROR",
        mutation_state: "NOT_ATTEMPTED",
        interleaving_applied: false,
        stage_obligations: 0,
        quarantine_obligations: 0,
        stage_obligation_ids: Vec::new(),
        quarantine_obligation_ids: Vec::new(),
        filesystem_clean: true,
        filesystem_postcondition: true,
        root_preserved: false,
        forensic_root: None,
        recovery_completed: true,
    }
}

fn run_reducer_only_case(
    boundary: FailureBoundary,
    interleaving: Interleaving,
    seed: u64,
    budget: &ExplorerBudget,
) -> ExplorerResult<ScenarioResult> {
    let real_probe = reducer_only_probe(boundary, interleaving);
    let draft = draft_for(boundary, interleaving, seed, real_probe);
    let original_count = draft.events.len();
    let minimized = minimize_draft(draft.clone(), boundary, interleaving, budget)?;
    let run = execute(&minimized, seed, budget)?;
    let invariant_complete = complete_invariants(&run, budget);
    let replay_fault_detected = invariant_complete
        && run.reducer.terminal() == Some(Outcome::Recovered)
        && run.reducer.operation_rejection_seen()
        && run.reducer.operation_recovery_seen()
        && !run.reducer.obligations().contains("operation-failure")
        && required_events_present(&minimized, boundary, interleaving);
    let unresolved_obligations = run.reducer.unresolved_obligations();
    let fault_detected = minimized.real_probe.fault_detected && replay_fault_detected;
    let safety_preserved = minimized.real_probe.safety_preserved && invariant_complete;
    let recovery_status = if !fault_detected {
        RecoveryStatus::Undetected
    } else if !safety_preserved {
        RecoveryStatus::Unsafe
    } else if !unresolved_obligations.is_empty() {
        RecoveryStatus::ForensicRequired
    } else {
        RecoveryStatus::Clean
    };
    let scenario_name = draft.scenario.clone();
    let diagnostic_error = (recovery_status != RecoveryStatus::Clean).then(|| {
        format!(
            "status={:?} reducer_only=true obligations={:?} invariant={} terminal={:?} reject={} recover={} events={}",
            recovery_status,
            unresolved_obligations,
            invariant_complete,
            run.reducer.terminal(),
            run.reducer.operation_rejection_seen(),
            run.reducer.operation_recovery_seen(),
            required_events_present(&minimized, boundary, interleaving),
        )
    });
    let trace = trace_json(&minimized, &run, budget);
    Ok(ScenarioResult {
        scenario: scenario_name,
        seed,
        failure_boundary: boundary.as_str().to_string(),
        boundary_execution: boundary.execution().to_string(),
        interleaving: interleaving.as_str().to_string(),
        fault_detected,
        recovery_status,
        safety_preserved,
        diagnostic_error,
        outcome: outcome_name(run.reducer.terminal().expect("terminal checked")).to_string(),
        event_count: original_count,
        minimized_event_count: minimized.events.len(),
        schedule_steps: minimized.schedule_steps,
        operation: minimized.real_probe.operation.as_str().to_string(),
        checkpoint: checkpoint_name(minimized.real_probe.checkpoint).to_string(),
        placement: minimized.real_probe.placement.as_str().to_string(),
        probe_result: minimized.real_probe.result.to_string(),
        real_checkpoint_observed: false,
        real_injection_observed: false,
        operation_outcome: minimized.real_probe.operation_outcome.to_string(),
        mutation_state: minimized.real_probe.mutation_state.to_string(),
        interleaving_applied: false,
        stage_obligations: 0,
        quarantine_obligations: 0,
        stage_obligation_ids: Vec::new(),
        quarantine_obligation_ids: Vec::new(),
        filesystem_clean: true,
        filesystem_postcondition: true,
        root_preserved: false,
        forensic_root: None,
        recovery_completed: unresolved_obligations.is_empty(),
        expected_identity: None,
        staged_identity: None,
        obligation_identity: None,
        typed_rejection_observed: run.reducer.operation_rejection_seen(),
        typed_recovery_observed: run.reducer.operation_recovery_seen(),
        unresolved_obligations,
        virtual_time_ns: *run.times.last().unwrap_or(&0),
        invariant_complete,
        trace,
    })
}

/// Run the full deterministic failure/interleaving matrix.
pub fn explore(seed: u64, budget: ExplorerBudget) -> ExplorerResult<ExplorerReport> {
    budget.validate()?;
    let mut scenarios = Vec::with_capacity(budget.max_scenarios);
    let mut ordinal = 0usize;
    'matrix: for boundary in FAILURE_BOUNDARIES {
        for interleaving in REAL_INTERLEAVINGS {
            if scenarios.len() == budget.max_scenarios {
                break 'matrix;
            }
            let scenario_seed = mix_seed(seed, ordinal as u64);
            let real_probe = run_real_probe(boundary.probe_spec(), interleaving, scenario_seed)?;
            let draft = draft_for(boundary, interleaving, scenario_seed, real_probe);
            let original_count = draft.events.len();
            let minimized = minimize_draft(draft.clone(), boundary, interleaving, &budget)?;
            let run = execute(&minimized, scenario_seed, &budget)?;
            let invariant_complete = complete_invariants(&run, &budget);
            let replay_fault_detected = invariant_complete
                && run.reducer.terminal() == Some(Outcome::Recovered)
                && run.reducer.operation_rejection_seen()
                && run.reducer.operation_recovery_seen()
                && !run.reducer.obligations().contains("operation-failure")
                && required_events_present(&minimized, boundary, interleaving);
            let fault_detected = minimized.real_probe.fault_detected && replay_fault_detected;
            let safety_preserved = minimized.real_probe.safety_preserved && invariant_complete;
            let recovery_status = if !fault_detected {
                RecoveryStatus::Undetected
            } else if !safety_preserved {
                RecoveryStatus::Unsafe
            } else if !run.reducer.unresolved_obligations().is_empty() {
                RecoveryStatus::ForensicRequired
            } else if minimized.real_probe.recovery_status == RecoveryStatus::Clean
                && minimized.real_probe.recovery_completed
                && minimized.real_probe.stage_obligations == 0
                && minimized.real_probe.quarantine_obligations == 0
                && minimized.real_probe.filesystem_clean
                && minimized.real_probe.filesystem_postcondition
                && !minimized.real_probe.root_preserved
            {
                RecoveryStatus::Clean
            } else {
                RecoveryStatus::ForensicRequired
            };
            let scenario_name = draft.scenario.clone();
            let diagnostic_error = (recovery_status != RecoveryStatus::Clean).then(|| {
                format!(
                    "status={:?} op={} mut={} fault={} hook={} obligations={:?} stage={} quarantine={} fs={} post={} preserved={} forensic_root={:?} invariant={} terminal={:?} reject={} recover={} events={}",
                    recovery_status,
                    minimized.real_probe.operation_outcome,
                    minimized.real_probe.mutation_state,
                    fault_detected,
                    minimized.real_probe.interleaving_applied,
                    run.reducer.obligations(),
                    minimized.real_probe.stage_obligations,
                    minimized.real_probe.quarantine_obligations,
                    minimized.real_probe.filesystem_clean,
                    minimized.real_probe.filesystem_postcondition,
                    minimized.real_probe.root_preserved,
                    minimized.real_probe.forensic_root,
                    invariant_complete,
                    run.reducer.terminal(),
                    run.reducer.operation_rejection_seen(),
                    run.reducer.operation_recovery_seen(),
                    required_events_present(&minimized, boundary, interleaving),
                )
            });
            let trace = trace_json(&minimized, &run, &budget);
            scenarios.push(ScenarioResult {
                scenario: scenario_name,
                seed: scenario_seed,
                failure_boundary: boundary.as_str().to_string(),
                boundary_execution: boundary.execution().to_string(),
                interleaving: interleaving.as_str().to_string(),
                fault_detected,
                recovery_status,
                safety_preserved,
                diagnostic_error,
                outcome: outcome_name(run.reducer.terminal().expect("terminal checked"))
                    .to_string(),
                event_count: original_count,
                minimized_event_count: minimized.events.len(),
                schedule_steps: minimized.schedule_steps,
                operation: minimized.real_probe.operation.as_str().to_string(),
                checkpoint: checkpoint_name(minimized.real_probe.checkpoint).to_string(),
                placement: minimized.real_probe.placement.as_str().to_string(),
                probe_result: minimized.real_probe.result.to_string(),
                real_checkpoint_observed: minimized.real_probe.real_checkpoint_observed,
                real_injection_observed: minimized.real_probe.real_injection_observed,
                operation_outcome: minimized.real_probe.operation_outcome.to_string(),
                mutation_state: minimized.real_probe.mutation_state.to_string(),
                interleaving_applied: minimized.real_probe.interleaving_applied,
                stage_obligations: minimized.real_probe.stage_obligations,
                quarantine_obligations: minimized.real_probe.quarantine_obligations,
                stage_obligation_ids: minimized.real_probe.stage_obligation_ids.clone(),
                quarantine_obligation_ids: minimized.real_probe.quarantine_obligation_ids.clone(),
                filesystem_clean: minimized.real_probe.filesystem_clean,
                filesystem_postcondition: minimized.real_probe.filesystem_postcondition,
                root_preserved: minimized.real_probe.root_preserved,
                forensic_root: minimized.real_probe.forensic_root.clone(),
                recovery_completed: minimized.real_probe.recovery_completed,
                expected_identity: minimized.real_probe.expected_identity,
                staged_identity: minimized.real_probe.staged_identity,
                obligation_identity: minimized.real_probe.obligation_identity,
                typed_rejection_observed: run.reducer.operation_rejection_seen(),
                typed_recovery_observed: run.reducer.operation_recovery_seen(),
                unresolved_obligations: run.reducer.unresolved_obligations(),
                virtual_time_ns: *run.times.last().unwrap_or(&0),
                invariant_complete,
                trace,
            });
            ordinal += 1;
        }
    }
    'reducer_only: for boundary in REDUCER_ONLY_FAILURE_BOUNDARIES {
        for interleaving in REDUCER_ONLY_INTERLEAVINGS {
            if scenarios.len() == budget.max_scenarios {
                break 'reducer_only;
            }
            let scenario_seed = mix_seed(seed, ordinal as u64);
            scenarios.push(run_reducer_only_case(
                boundary,
                interleaving,
                scenario_seed,
                &budget,
            )?);
            ordinal += 1;
        }
    }
    let real_scenario_count = scenarios
        .iter()
        .filter(|scenario| scenario.boundary_execution == "REAL_OPERATION_CHECKPOINT")
        .count();
    let reducer_only_scenario_count = scenarios
        .iter()
        .filter(|scenario| {
            REDUCER_ONLY_INTERLEAVINGS
                .iter()
                .any(|interleaving| interleaving.as_str() == scenario.interleaving)
        })
        .count();
    let reducer_only_alias_scenario_count = scenarios
        .len()
        .saturating_sub(real_scenario_count + reducer_only_scenario_count);
    let base_matrix_clean_count = scenarios
        .iter()
        .filter(|scenario| {
            REAL_INTERLEAVINGS
                .iter()
                .any(|interleaving| interleaving.as_str() == scenario.interleaving)
                && scenario.recovery_status == RecoveryStatus::Clean
        })
        .count();
    let base_matrix_forensic_count = scenarios
        .iter()
        .filter(|scenario| {
            REAL_INTERLEAVINGS
                .iter()
                .any(|interleaving| interleaving.as_str() == scenario.interleaving)
                && scenario.recovery_status == RecoveryStatus::ForensicRequired
        })
        .count();
    let reducer_only_matrix_clean_count = scenarios
        .iter()
        .filter(|scenario| {
            REDUCER_ONLY_INTERLEAVINGS
                .iter()
                .any(|interleaving| interleaving.as_str() == scenario.interleaving)
                && scenario.recovery_status == RecoveryStatus::Clean
        })
        .count();
    let reducer_only_matrix_forensic_count = scenarios
        .iter()
        .filter(|scenario| {
            REDUCER_ONLY_INTERLEAVINGS
                .iter()
                .any(|interleaving| interleaving.as_str() == scenario.interleaving)
                && scenario.recovery_status == RecoveryStatus::ForensicRequired
        })
        .count();
    Ok(ExplorerReport {
        explorer_version: EXPLORER_VERSION,
        seed,
        budget,
        declared_failure_boundaries: FAILURE_BOUNDARIES
            .iter()
            .map(|boundary| boundary.as_str().to_string())
            .collect(),
        real_failure_boundaries: REAL_FAILURE_BOUNDARIES
            .iter()
            .map(|boundary| boundary.as_str().to_string())
            .collect(),
        reducer_only_failure_boundaries: REDUCER_ONLY_FAILURE_BOUNDARIES
            .iter()
            .map(|boundary| boundary.as_str().to_string())
            .collect(),
        representative_interleavings: REAL_INTERLEAVINGS
            .iter()
            .map(|interleaving| interleaving.as_str().to_string())
            .collect(),
        reducer_only_interleavings: REDUCER_ONLY_INTERLEAVINGS
            .iter()
            .map(|interleaving| interleaving.as_str().to_string())
            .collect(),
        declared_operation_checkpoints: OPERATION_CHECKPOINTS
            .iter()
            .map(|checkpoint| checkpoint_name(*checkpoint).to_string())
            .collect(),
        scenario_count: scenarios.len(),
        real_scenario_count,
        reducer_only_alias_scenario_count,
        reducer_only_scenario_count,
        clean_count: scenarios
            .iter()
            .filter(|scenario| scenario.recovery_status == RecoveryStatus::Clean)
            .count(),
        forensic_count: scenarios
            .iter()
            .filter(|scenario| scenario.recovery_status == RecoveryStatus::ForensicRequired)
            .count(),
        unsafe_count: scenarios
            .iter()
            .filter(|scenario| scenario.recovery_status == RecoveryStatus::Unsafe)
            .count(),
        undetected_count: scenarios
            .iter()
            .filter(|scenario| scenario.recovery_status == RecoveryStatus::Undetected)
            .count(),
        base_matrix_clean_count,
        base_matrix_forensic_count,
        reducer_only_matrix_clean_count,
        reducer_only_matrix_forensic_count,
        scenarios,
    })
}

fn mix_seed(seed: u64, ordinal: u64) -> u64 {
    let mut value = seed ^ ordinal.wrapping_mul(0x9e37_79b9_7f4a_7c15);
    value ^= value >> 30;
    value = value.wrapping_mul(0xbf58_476d_1ce4_e5b9);
    value ^= value >> 27;
    value = value.wrapping_mul(0x94d0_49bb_1331_11eb);
    value ^ (value >> 31)
}

fn push_event(
    events: &mut Vec<DraftEvent>,
    actor: &'static str,
    event: Event,
    data: Value,
    seed: &mut u64,
) {
    *seed = mix_seed(*seed, events.len() as u64 + 1);
    let delta_ns = 100 + (*seed % 300);
    events.push(DraftEvent {
        actor,
        event,
        data,
        delta_ns,
    });
}

fn draft_for(
    boundary: FailureBoundary,
    interleaving: Interleaving,
    seed: u64,
    real_probe: RealProbe,
) -> Draft {
    let initial_stable = match interleaving {
        Interleaving::PartialPublication => StableState::Uninitialized,
        Interleaving::Restart => StableState::Ready,
        _ => StableState::Running,
    };
    let catalog = vec![
        CatalogSpec {
            id: "cap.session-root",
            kind: CapabilityKind::SessionRootPidfd,
            token: "session-root@explorer",
        },
        CatalogSpec {
            id: "cap.launchd-member",
            kind: CapabilityKind::SessionMemberPidfd,
            token: "launchd@explorer",
        },
        CatalogSpec {
            id: "cap.late-member",
            kind: CapabilityKind::SessionMemberPidfd,
            token: "late-member@explorer",
        },
        CatalogSpec {
            id: "cap.shared-lease",
            kind: CapabilityKind::SharedSessionLease,
            token: "lease@explorer",
        },
        CatalogSpec {
            id: "cap.launchd-endpoint",
            kind: CapabilityKind::RuntimeEndpoint,
            token: "launchd-socket@explorer",
        },
    ];
    let mut events = Vec::new();
    let mut event_seed = seed;
    let intent = match interleaving {
        Interleaving::PartialPublication => IntentKind::CreatePrefix,
        Interleaving::Restart => IntentKind::StartSession,
        _ => IntentKind::RequestShutdown,
    };
    push_event(
        &mut events,
        "launcher",
        Event::Intent {
            intent,
            transaction: format!("txn.explorer-{}", boundary.as_str()),
        },
        json!({
            "intent": intent_name(intent),
            "transaction_id": format!("txn.explorer-{}", boundary.as_str()),
        }),
        &mut event_seed,
    );
    if !matches!(
        interleaving,
        Interleaving::PartialPublication | Interleaving::Restart
    ) {
        push_event(
            &mut events,
            "controller",
            Event::BarrierEntered,
            json!({"barrier": "THREADS_STOPPED", "result": "ENTERED"}),
            &mut event_seed,
        );
        acquire(&mut events, &catalog[0], "controller", &mut event_seed);
        acquire(&mut events, &catalog[1], "controller", &mut event_seed);
        push_event(
            &mut events,
            "observer",
            Event::Membership {
                members: vec![catalog[0].id.to_string(), catalog[1].id.to_string()],
                completeness: if interleaving == Interleaving::OrphanCgroup {
                    "TIMEOUT".to_string()
                } else {
                    "CLOSED".to_string()
                },
            },
            json!({
                "authority": if interleaving == Interleaving::OrphanCgroup {
                    "DELEGATED_CGROUP"
                } else {
                    "SESSION_LEDGER"
                },
                "members": [catalog[0].id, catalog[1].id],
                "completeness": if interleaving == Interleaving::OrphanCgroup { "TIMEOUT" } else { "CLOSED" },
            }),
            &mut event_seed,
        );
        add_interleaving_events(&mut events, interleaving, &catalog, &mut event_seed);
        if matches!(
            interleaving,
            Interleaving::LateFork | Interleaving::ForkChurn
        ) {
            push_event(
                &mut events,
                "controller",
                Event::Fault {
                    name: "LATE_FORK".to_string(),
                },
                json!({"phase": "BARRIER", "fault": "LATE_FORK"}),
                &mut event_seed,
            );
        }
    } else if interleaving == Interleaving::Restart {
        push_event(
            &mut events,
            "observer",
            Event::Endpoint {
                endpoint: "endpoint.launchd".to_string(),
                operation: "REVALIDATE".to_string(),
                result: "ALREADY_GONE".to_string(),
            },
            json!({
                "endpoint": "endpoint.launchd",
                "operation": "REVALIDATE",
                "result": "ALREADY_GONE"
            }),
            &mut event_seed,
        );
    }
    push_event(
        &mut events,
        "controller",
        Event::OperationCheckpoint {
            operation: real_probe.operation.as_str().to_string(),
            checkpoint: checkpoint_name(real_probe.checkpoint).to_string(),
            placement: real_probe.placement.as_str().to_string(),
            result: real_probe.result.to_string(),
        },
        json!({
            "operation": real_probe.operation.as_str(),
            "checkpoint": checkpoint_name(real_probe.checkpoint),
            "placement": real_probe.placement.as_str(),
            "result": real_probe.result,
        }),
        &mut event_seed,
    );
    push_event(
        &mut events,
        "controller",
        Event::Fault {
            name: boundary.fault().to_string(),
        },
        json!({
            "phase": boundary.phase(),
            "fault": boundary.fault()
        }),
        &mut event_seed,
    );
    let recovery = match interleaving {
        Interleaving::PartialPublication => RecoveryAction::RollbackUninitialized,
        Interleaving::Restart => RecoveryAction::RollbackReady,
        Interleaving::PostDeadlineSignal => RecoveryAction::ContinueDrain,
        _ => RecoveryAction::RollbackRunning,
    };
    push_event(
        &mut events,
        "recovery",
        Event::Recovery(recovery),
        json!({
            "action": recovery_name(recovery),
            "reason": "explorer-fault"
        }),
        &mut event_seed,
    );
    push_event(
        &mut events,
        "recovery",
        Event::Terminal(Outcome::Recovered),
        json!({"outcome": "RECOVERED"}),
        &mut event_seed,
    );
    Draft {
        scenario: format!("{}-{}", boundary.as_str(), interleaving.as_str()),
        initial_stable,
        catalog,
        events,
        schedule_steps: schedule_steps(interleaving),
        real_probe,
    }
}

fn schedule_steps(interleaving: Interleaving) -> usize {
    match interleaving {
        Interleaving::None => 0,
        Interleaving::OrphanCgroup
        | Interleaving::PostDeadlineSignal
        | Interleaving::InodeAba
        | Interleaving::PartialPublication
        | Interleaving::Restart
        | Interleaving::CleanupFailure
        | Interleaving::RootExit
        | Interleaving::PidReuse
        | Interleaving::ConcurrentClose => 1,
        Interleaving::LateFork => 2,
        Interleaving::ForkChurn => 3,
    }
}

fn acquire(
    events: &mut Vec<DraftEvent>,
    capability: &CatalogSpec,
    owner: &'static str,
    seed: &mut u64,
) {
    push_event(
        events,
        "controller",
        Event::Acquire {
            id: capability.id.to_string(),
            kind: capability.kind,
            generation: 1,
            owner: owner.to_string(),
        },
        json!({"capability": capability.id, "owner": owner}),
        seed,
    );
}

fn add_interleaving_events(
    events: &mut Vec<DraftEvent>,
    interleaving: Interleaving,
    catalog: &[CatalogSpec],
    seed: &mut u64,
) {
    match interleaving {
        Interleaving::PostDeadlineSignal => {
            push_event(
                events,
                "controller",
                Event::Identity {
                    id: catalog[0].id.to_string(),
                    matches: true,
                },
                json!({"capability": catalog[0].id, "result": "MATCH"}),
                seed,
            );
            push_event(
                events,
                "controller",
                Event::Signal {
                    id: catalog[0].id.to_string(),
                    result: SignalResult::Deadline,
                },
                json!({
                    "capability": catalog[0].id,
                    "signal": "TERM",
                    "result": "DEADLINE"
                }),
                seed,
            );
        }
        Interleaving::InodeAba | Interleaving::PidReuse | Interleaving::RootExit => {
            let result = match interleaving {
                Interleaving::RootExit => "GONE",
                _ => "MISMATCH",
            };
            push_event(
                events,
                "controller",
                Event::Identity {
                    id: catalog[0].id.to_string(),
                    matches: false,
                },
                json!({"capability": catalog[0].id, "result": result}),
                seed,
            );
        }
        Interleaving::LateFork => {
            acquire(events, &catalog[2], "controller", seed);
            push_event(
                events,
                "member",
                Event::MemberObserved {
                    id: catalog[2].id.to_string(),
                    origin: "LATE_FORK".to_string(),
                },
                json!({"capability": catalog[2].id, "origin": "LATE_FORK"}),
                seed,
            );
        }
        Interleaving::ForkChurn => {
            acquire(events, &catalog[2], "controller", seed);
            push_event(
                events,
                "member",
                Event::MemberObserved {
                    id: catalog[2].id.to_string(),
                    origin: "LATE_FORK".to_string(),
                },
                json!({"capability": catalog[2].id, "origin": "LATE_FORK"}),
                seed,
            );
            push_event(
                events,
                "member",
                Event::MemberObserved {
                    id: catalog[2].id.to_string(),
                    origin: "SNAPSHOT".to_string(),
                },
                json!({"capability": catalog[2].id, "origin": "SNAPSHOT"}),
                seed,
            );
        }
        Interleaving::ConcurrentClose => {
            push_event(
                events,
                "observer",
                Event::Endpoint {
                    endpoint: "endpoint.launchd".to_string(),
                    operation: "CLOSE".to_string(),
                    result: "REMOVED".to_string(),
                },
                json!({
                    "endpoint": "endpoint.launchd",
                    "operation": "CLOSE",
                    "result": "REMOVED"
                }),
                seed,
            );
        }
        Interleaving::CleanupFailure => {
            push_event(
                events,
                "observer",
                Event::Endpoint {
                    endpoint: "endpoint.launchd".to_string(),
                    operation: "UNLINK".to_string(),
                    result: "REJECTED".to_string(),
                },
                json!({
                    "endpoint": "endpoint.launchd",
                    "operation": "UNLINK",
                    "result": "REJECTED"
                }),
                seed,
            );
        }
        Interleaving::OrphanCgroup
        | Interleaving::PartialPublication
        | Interleaving::Restart
        | Interleaving::None => {}
    }
}

fn minimize_draft(
    mut draft: Draft,
    boundary: FailureBoundary,
    interleaving: Interleaving,
    budget: &ExplorerBudget,
) -> ExplorerResult<Draft> {
    let mut index = draft.events.len().saturating_sub(1);
    while index > 0 {
        if matches!(draft.events[index].event, Event::Terminal(_)) {
            index -= 1;
            continue;
        }
        let mut candidate = draft.clone();
        candidate.events.remove(index);
        let keeps_detection = execute(&candidate, 0x5eed, budget).ok().is_some_and(|run| {
            complete_invariants(&run, budget)
                && run.reducer.terminal() == Some(Outcome::Recovered)
                && required_events_present(&candidate, boundary, interleaving)
        });
        if keeps_detection {
            draft = candidate;
        }
        index -= 1;
    }
    Ok(draft)
}

fn execute(draft: &Draft, seed: u64, budget: &ExplorerBudget) -> ExplorerResult<Run> {
    if draft.events.len() < 2 || draft.events.len() > budget.max_events {
        return Err(ExplorerError::Budget("events"));
    }
    if draft.schedule_steps > budget.max_schedule_steps {
        return Err(ExplorerError::Budget("schedule steps"));
    }
    if !matches!(
        draft.events.last().map(|event| &event.event),
        Some(Event::Terminal(_))
    ) || draft
        .events
        .iter()
        .take(draft.events.len() - 1)
        .any(|event| matches!(event.event, Event::Terminal(_)))
    {
        return Err(ExplorerError::Reducer(
            "terminal must be unique and last".to_string(),
        ));
    }
    let mut reducer = Reducer::new(draft.initial_stable);
    for capability in &draft.catalog {
        reducer
            .register_capability(capability.id, capability.kind, 1)
            .map_err(|error| ExplorerError::Reducer(error.to_string()))?;
    }
    let mut clock = VirtualClock::new(budget.max_virtual_time_ns)?;
    let mut snapshots = Vec::with_capacity(draft.events.len());
    let mut times = Vec::with_capacity(draft.events.len());
    let mut schedule_seed = seed;
    for event in &draft.events {
        schedule_seed = mix_seed(schedule_seed, times.len() as u64 + 1);
        let schedule_delta = event.delta_ns + (schedule_seed % 7);
        clock.advance(schedule_delta)?;
        reducer
            .apply(event.event.clone())
            .map_err(|error| ExplorerError::Reducer(error.to_string()))?;
        if reducer.live().len() > budget.max_live_capabilities
            || reducer.observations().len() > budget.max_recovery_steps
        {
            return Err(ExplorerError::Budget("per-step reducer state"));
        }
        snapshots.push(reducer.snapshot().clone());
        times.push(clock.now_ns());
        verify_step_invariants(&reducer, snapshots.len(), clock.now_ns(), budget)?;
    }
    Ok(Run {
        reducer,
        snapshots,
        times,
    })
}

/// Safety properties must hold after every accepted event.  Two registry
/// entries are intentionally completion properties: `terminal-trace` can
/// only be true at the unique final event, and
/// `late-fork-closes-snapshot` is allowed to remain unresolved until the
/// corresponding fault/recovery pair is consumed.  The final check below
/// still requires the complete registry.
fn verify_step_invariants(
    reducer: &Reducer,
    event_count: usize,
    time_ns: u64,
    budget: &ExplorerBudget,
) -> ExplorerResult<()> {
    const STEP_INVARIANTS: &[&str] = &[
        "no-raw-path-authority",
        "no-unvalidated-fd",
        "stable-journal-independent",
        "total-recovery-matrix",
        "identity-before-signal",
        "no-children-after-gone",
        "shared-lease-bound",
        "pidfd-identity-before-signal",
        "bounded-replay",
    ];
    let satisfied = reducer.invariant_names(
        event_count,
        time_ns,
        budget.max_events,
        budget.max_virtual_time_ns,
        budget.max_live_capabilities,
        budget.max_recovery_steps,
    );
    if let Some(missing) = STEP_INVARIANTS
        .iter()
        .find(|invariant| !satisfied.contains(**invariant))
    {
        return Err(ExplorerError::Invariant(format!(
            "step {event_count} missing safety invariant {missing}"
        )));
    }
    Ok(())
}

fn required_events_present(
    draft: &Draft,
    boundary: FailureBoundary,
    interleaving: Interleaving,
) -> bool {
    let probe_present = draft.events.iter().any(|event| {
        matches!(
            &event.event,
            Event::OperationCheckpoint {
                operation,
                checkpoint,
                placement,
                result,
            } if operation == draft.real_probe.operation.as_str()
                && checkpoint == checkpoint_name(draft.real_probe.checkpoint)
                && placement == draft.real_probe.placement.as_str()
                && result == draft.real_probe.result
        )
    });
    let fault_present = draft
        .events
        .iter()
        .any(|event| matches!(&event.event, Event::Fault { name } if name == boundary.fault()));
    let recovery_present = draft
        .events
        .iter()
        .any(|event| matches!(event.event, Event::Recovery(_)));
    let interleaving_present = match interleaving {
        Interleaving::None | Interleaving::PartialPublication | Interleaving::Restart => true,
        Interleaving::PostDeadlineSignal => draft
            .events
            .iter()
            .any(|event| matches!(event.event, Event::Signal { .. })),
        Interleaving::InodeAba | Interleaving::PidReuse | Interleaving::RootExit => draft
            .events
            .iter()
            .any(|event| matches!(event.event, Event::Identity { matches: false, .. })),
        Interleaving::OrphanCgroup => draft.events.iter().any(|event| {
            matches!(&event.event, Event::Membership { completeness, .. } if completeness == "TIMEOUT")
        }),
        Interleaving::LateFork | Interleaving::ForkChurn => draft.events.iter().any(|event| {
            matches!(&event.event, Event::MemberObserved { origin, .. } if origin == "LATE_FORK")
        }),
        Interleaving::CleanupFailure | Interleaving::ConcurrentClose => draft.events.iter().any(
            |event| matches!(event.event, Event::Endpoint { .. }),
        ),
    };
    probe_present && fault_present && recovery_present && interleaving_present
}

fn complete_invariants(run: &Run, budget: &ExplorerBudget) -> bool {
    let satisfied = run.reducer.invariant_names(
        run.snapshots.len(),
        *run.times.last().unwrap_or(&0),
        budget.max_events,
        budget.max_virtual_time_ns,
        budget.max_live_capabilities,
        budget.max_recovery_steps,
    );
    INVARIANT_REGISTRY
        .iter()
        .all(|invariant| satisfied.contains(*invariant))
}

fn trace_json(draft: &Draft, run: &Run, budget: &ExplorerBudget) -> Value {
    let trace_id = draft.scenario.clone();
    let catalog: Vec<Value> = draft
        .catalog
        .iter()
        .map(|capability| {
            json!({
                "capability_id": capability.id,
                "kind": capability_kind_name(capability.kind),
                "identity": {"token": capability.token, "generation": 1}
            })
        })
        .collect();
    let events: Vec<Value> = draft
        .events
        .iter()
        .enumerate()
        .map(|(index, event)| {
            json!({
                "seq": index,
                "time_ns": run.times[index],
                "actor": event.actor,
                "kind": event_kind_name(&event.event),
                "data": event.data,
                "state_after": snapshot_json(run.snapshots[index].clone())
            })
        })
        .collect();
    let expected_invariants: Vec<&str> = INVARIANT_REGISTRY.to_vec();
    let live_capabilities: Vec<Value> = run
        .reducer
        .live()
        .iter()
        .map(|(capability, owner)| json!({"capability": capability, "owner": owner}))
        .collect();
    let recovery_observations: Vec<Value> = run
        .reducer
        .observations()
        .iter()
        .map(|observation| {
            json!({
                "stable_state": stable_name(observation.stable),
                "journal_phase": journal_name(observation.journal),
                "action": recovery_name(observation.action)
            })
        })
        .collect();
    json!({
        "schema_version": 1,
        "kind": "lifecycle-replay-trace",
        "model_version": 1,
        "trace_id": trace_id,
        "scenario": trace_id,
        "provenance": {
            "kind": "golden-scenario",
            "source_label": "dar-4ush.3-deterministic-explorer",
            "evidence_id": format!("dar-4ush.3/{trace_id}"),
            "source_identity": {
                "repository": "darling-workspace",
                "commit": EXPLORER_SOURCE_COMMIT,
                "tree": EXPLORER_SOURCE_TREE,
                "profile": "lifecycle-lab",
                "module": "operation-boundary"
            }
        },
        "budget": {
            "max_events": budget.max_events,
            "max_virtual_time_ns": budget.max_virtual_time_ns,
            "max_live_capabilities": budget.max_live_capabilities,
            "max_recovery_steps": budget.max_recovery_steps
        },
        "initial": {
            "snapshot": snapshot_json(StateSnapshot {stable: draft.initial_stable, journal: JournalPhase::None, intent: IntentKind::None}),
            "capability_catalog": catalog,
            "live_capabilities": []
        },
        "events": events,
        "expected": {
            "outcome": outcome_name(run.reducer.terminal().unwrap_or(Outcome::Recovered)),
            "stable_state": stable_name(run.reducer.snapshot().stable),
            "journal_phase": journal_name(run.reducer.snapshot().journal),
            "intent": intent_name(run.reducer.snapshot().intent),
            "terminal_event_kind": "terminal",
            "invariants": expected_invariants,
            "live_capabilities": live_capabilities
        },
        "recovery_observations": recovery_observations
    })
}

fn event_kind_name(event: &Event) -> &'static str {
    match event {
        Event::Acquire { .. } => "capability_acquired",
        Event::Move { .. } => "capability_moved",
        Event::Release { .. } => "capability_released",
        Event::Intent { .. } => "intent_declared",
        Event::BarrierEntered => "barrier_entered",
        Event::Membership { .. } => "membership_snapshot",
        Event::Identity { .. } => "identity_revalidated",
        Event::Signal { .. } => "signal_sent",
        Event::Endpoint { .. } => "endpoint_transition",
        Event::OperationCheckpoint { .. } => "operation_checkpoint",
        Event::MemberObserved { .. } => "member_observed",
        Event::Fault { .. } => "fault_injected",
        Event::Recovery(_) => "recovery",
        Event::Terminal(_) => "terminal",
    }
}

fn checkpoint_name(checkpoint: Checkpoint) -> &'static str {
    match checkpoint {
        Checkpoint::AfterMkdirBeforeBind => "after-mkdir-before-bind",
        Checkpoint::AfterBindBeforePublish => "after-bind-before-publish",
        Checkpoint::AfterStageMkdirBeforeBind => "after-stage-mkdir-before-bind",
        Checkpoint::AfterStageBindBeforeMove => "after-stage-bind-before-move",
        Checkpoint::AfterMoveBeforeVerify => "after-move-before-verify",
        Checkpoint::AfterQuarantineMoveBeforeVerify => "after-quarantine-move-before-verify",
        Checkpoint::AfterQuarantineVerifyBeforeGc => "after-quarantine-verify-before-gc",
        Checkpoint::BeforeExactMutation => "before-exact-mutation",
        Checkpoint::AfterExactMutation => "after-exact-mutation",
    }
}

fn capability_kind_name(kind: CapabilityKind) -> &'static str {
    match kind {
        CapabilityKind::PrefixDirectory => "PREFIX_DIRECTORY",
        CapabilityKind::SessionRootPidfd => "SESSION_ROOT_PIDFD",
        CapabilityKind::SessionMemberPidfd => "SESSION_MEMBER_PIDFD",
        CapabilityKind::SharedSessionLease => "SHARED_SESSION_LEASE",
        CapabilityKind::RuntimeEndpoint => "RUNTIME_ENDPOINT",
        CapabilityKind::JournalRecord => "JOURNAL_RECORD",
    }
}

fn stable_name(stable: StableState) -> &'static str {
    match stable {
        StableState::Uninitialized => "UNINITIALIZED",
        StableState::Ready => "READY",
        StableState::Running => "RUNNING",
        StableState::Draining => "DRAINING",
        StableState::Stopped => "STOPPED",
        StableState::Corrupt => "CORRUPT",
    }
}

fn journal_name(journal: JournalPhase) -> &'static str {
    match journal {
        JournalPhase::None => "NONE",
        JournalPhase::Prepare => "PREPARE",
        JournalPhase::Publish => "PUBLISH",
        JournalPhase::Cleanup => "CLEANUP",
        JournalPhase::Commit => "COMMIT",
        JournalPhase::Abort => "ABORT",
    }
}

fn intent_name(intent: IntentKind) -> &'static str {
    match intent {
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

fn outcome_name(outcome: Outcome) -> &'static str {
    match outcome {
        Outcome::Success => "SUCCESS",
        Outcome::FailClosed => "FAIL_CLOSED",
        Outcome::Timeout => "TIMEOUT",
        Outcome::Recovered => "RECOVERED",
    }
}

fn snapshot_json(snapshot: StateSnapshot) -> Value {
    json!({
        "stable_state": stable_name(snapshot.stable),
        "journal_phase": journal_name(snapshot.journal),
        "intent": intent_name(snapshot.intent)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{ScopeId, StageRef, UnboundQuarantine, UnboundStageObligation};
    use std::collections::BTreeSet;
    use std::ffi::CString;

    fn test_identity(id: u64) -> FileIdentity {
        FileIdentity {
            device: 1,
            inode: id,
            mode: libc::S_IFREG,
            nlink: 1,
            uid: 0,
            gid: 0,
        }
    }

    fn test_quarantine(id: u64) -> QuarantineObligation {
        QuarantineObligation::Unbound(UnboundQuarantine {
            parent_fd: None,
            parent_identity: test_identity(1000),
            lease_identity: test_identity(1001),
            scope: ScopeId(1),
            quarantine_name: CString::new(format!("quarantine-{id}"))
                .expect("test name has no NUL"),
            expected: test_identity(id),
            directory: false,
            id,
        })
    }

    fn test_stage(id: u64) -> StageObligation {
        StageObligation::Unbound(UnboundStageObligation {
            parent_fd: None,
            parent_identity: test_identity(2000),
            stage_ref: StageRef::new(id),
            child: None,
            name: CString::new(format!("stage-{id}")).expect("test name has no NUL"),
            expected: Some(test_identity(id)),
            child_identity: None,
            scope: ScopeId(2),
        })
    }

    #[test]
    fn quarantine_recovery_preserves_first_and_unprocessed_tail() {
        let queue = vec![
            test_quarantine(11),
            test_quarantine(12),
            test_quarantine(13),
        ];
        let error = drain_recovery_queue(queue, |item| {
            if item.id() == 11 {
                Err(Box::new((item, "first-quarantine-fault")))
            } else {
                Ok(())
            }
        })
        .expect_err("first quarantine failure must stop recovery");
        assert_eq!(error.0, "first-quarantine-fault");
        assert_eq!(
            error
                .1
                .iter()
                .map(QuarantineObligation::id)
                .collect::<Vec<_>>(),
            vec![11, 12, 13]
        );
    }

    #[test]
    fn stage_recovery_preserves_middle_and_unprocessed_tail() {
        let queue = vec![
            test_stage(21),
            test_stage(22),
            test_stage(23),
            test_stage(24),
        ];
        let error = drain_recovery_queue(queue, |item| {
            if item.id() == 22 {
                Err(Box::new((item, "middle-stage-fault")))
            } else {
                Ok(())
            }
        })
        .expect_err("middle stage failure must stop recovery");
        assert_eq!(error.0, "middle-stage-fault");
        assert_eq!(
            error.1.iter().map(StageObligation::id).collect::<Vec<_>>(),
            vec![22, 23, 24]
        );
    }

    #[test]
    fn quarantine_recovery_preserves_middle_and_unprocessed_tail() {
        let queue = vec![
            test_quarantine(31),
            test_quarantine(32),
            test_quarantine(33),
        ];
        let error = drain_recovery_queue(queue, |item| {
            if item.id() == 32 {
                Err(Box::new((item, "middle-quarantine-fault")))
            } else {
                Ok(())
            }
        })
        .expect_err("middle quarantine failure must stop recovery");
        assert_eq!(error.0, "middle-quarantine-fault");
        assert_eq!(
            error
                .1
                .iter()
                .map(QuarantineObligation::id)
                .collect::<Vec<_>>(),
            vec![32, 33]
        );
    }

    #[test]
    fn stage_recovery_preserves_first_and_unprocessed_tail() {
        let queue = vec![test_stage(41), test_stage(42), test_stage(43)];
        let error = drain_recovery_queue(queue, |item| {
            if item.id() == 41 {
                Err(Box::new((item, "first-stage-fault")))
            } else {
                Ok(())
            }
        })
        .expect_err("first stage failure must stop recovery");
        assert_eq!(error.0, "first-stage-fault");
        assert_eq!(
            error.1.iter().map(StageObligation::id).collect::<Vec<_>>(),
            vec![41, 42, 43]
        );
    }

    #[test]
    fn matrix_registry_is_unique_and_bounded() {
        let boundaries: BTreeSet<_> = FAILURE_BOUNDARIES
            .iter()
            .map(|item| item.as_str())
            .collect();
        let interleavings: BTreeSet<_> = REAL_INTERLEAVINGS
            .iter()
            .map(|item| item.as_str())
            .collect();
        assert_eq!(boundaries.len(), FAILURE_BOUNDARIES.len());
        assert_eq!(interleavings.len(), REAL_INTERLEAVINGS.len());
        ExplorerBudget::default()
            .validate()
            .expect("default budget");
    }

    #[test]
    fn explorer_is_deterministic_and_minimizes() {
        let budget = ExplorerBudget {
            max_scenarios: 8,
            ..ExplorerBudget::default()
        };
        let first = explore(0x1234, budget.clone()).expect("first exploration");
        let second = explore(0x1234, budget).expect("second exploration");
        let first = serde_json::to_string(&first).expect("serialize first");
        let second = serde_json::to_string(&second).expect("serialize second");
        assert_eq!(first, second);
        assert!(first.contains("minimized_event_count"));
    }

    #[test]
    fn default_matrix_reports_clean_and_forensic_cases() {
        let report = explore(7, ExplorerBudget::default()).expect("full matrix");
        assert_eq!(report.scenario_count, 16 * 4 + 8 * 8);
        assert_eq!(report.real_scenario_count, 8 * 4);
        assert_eq!(report.reducer_only_alias_scenario_count, 8 * 4);
        assert_eq!(report.reducer_only_scenario_count, 8 * 8);
        assert_eq!(report.base_matrix_clean_count, 56);
        assert_eq!(report.base_matrix_forensic_count, 8);
        assert_eq!(report.reducer_only_matrix_clean_count, 48);
        assert_eq!(report.reducer_only_matrix_forensic_count, 16);
        assert_eq!(
            report.clean_count,
            report
                .scenarios
                .iter()
                .filter(|scenario| scenario.recovery_status == RecoveryStatus::Clean)
                .count()
        );
        assert_eq!(report.clean_count, 104);
        assert_eq!(report.forensic_count, 24);
        assert_eq!(report.unsafe_count, 0);
        assert_eq!(report.undetected_count, 0);
        assert!(report.scenarios.iter().all(|scenario| {
            scenario.invariant_complete
                && scenario.fault_detected
                && scenario.safety_preserved
                && matches!(
                    scenario.recovery_status,
                    RecoveryStatus::Clean | RecoveryStatus::ForensicRequired
                )
        }));
    }
}
