//! Rust-owned Rootless lifecycle controller architecture for dar-4ush.7.
//!
//! This module is an architecture gate, not production routing.  The public
//! surface deliberately contains only an anchored request and immutable
//! observations.  Acquired descriptors, process identities, signal evidence,
//! quiescence proofs, and cleanup seams are Rust-owned and sealed.  Python is
//! limited to bounded JSON transport.

#![allow(dead_code)]
#![allow(clippy::result_large_err)]

use crate::FileIdentity;
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fmt;
use std::os::fd::{AsRawFd, OwnedFd, RawFd};
use std::sync::atomic::{AtomicU64, Ordering};

pub const CONTROLLER_SCHEMA_VERSION: u32 = 1;
pub const MAX_TRANSACTION_ID_BYTES: usize = 128;
pub const MAX_MEMBERS: usize = 64;
pub const MAX_ENDPOINTS: usize = 16;
pub const MAX_NONCE_BYTES: usize = 64;
pub const MAX_RUNTIME_DIGEST_BYTES: usize = 64;
pub const MAX_CONTROLLER_CLOSURE_BYTES: usize = 64;

/// A build-time replacement may provide the semantic closure digest.  The
/// architecture binary refuses an unbound digest rather than silently
/// accepting an arbitrary executable.
pub const UNBOUND_CONTROLLER_CLOSURE: &str =
    "0000000000000000000000000000000000000000000000000000000000000000";

fn compiled_controller_closure() -> &'static str {
    option_env!("LIFECYCLE_CONTROLLER_CLOSURE_SHA256").unwrap_or(UNBOUND_CONTROLLER_CLOSURE)
}

static NEXT_SCOPE: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct ScopeId(u64);

fn fresh_scope() -> ScopeId {
    ScopeId(NEXT_SCOPE.fetch_add(1, Ordering::Relaxed))
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ControllerOperation {
    RequestShutdown,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum ControllerProfile {
    Rootless,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ControllerBudget {
    pub max_events: u32,
    pub max_virtual_time_ns: u64,
    pub max_members: u32,
    pub max_recovery_steps: u32,
    pub deadline_ns: u64,
}

impl ControllerBudget {
    pub fn validate(self) -> Result<Self, ControllerError> {
        if self.max_events == 0
            || self.max_events > 128
            || self.max_virtual_time_ns == 0
            || self.max_virtual_time_ns > 1_000_000
            || self.max_members == 0
            || self.max_members as usize > MAX_MEMBERS
            || self.max_recovery_steps == 0
            || self.max_recovery_steps > 16
            || self.deadline_ns == 0
        {
            return Err(ControllerError::BudgetExceeded);
        }
        Ok(self)
    }
}

/// The request contains only an inherited anchor/evidence capability.  A
/// launcher descriptor is intentionally absent: the Rust acquisition backend
/// opens launcher, marker, init and pidfds relative to the retained anchor.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ControllerRequest {
    pub schema_version: u32,
    pub transaction_id: String,
    pub profile: ControllerProfile,
    pub operation: ControllerOperation,
    pub anchor_fd: RawFd,
    pub evidence_fd: Option<RawFd>,
    pub controller_closure_sha256: String,
    pub runtime_identity_digest: String,
    pub request_nonce: String,
    pub budget: ControllerBudget,
}

fn valid_hex(value: &str, bytes: usize) -> bool {
    value.len() == bytes
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

impl ControllerRequest {
    pub fn validate(&self) -> Result<(), ControllerError> {
        if self.schema_version != CONTROLLER_SCHEMA_VERSION
            || self.transaction_id.is_empty()
            || self.transaction_id.len() > MAX_TRANSACTION_ID_BYTES
            || self.anchor_fd < 0
            || self.evidence_fd.is_some_and(|fd| fd < 0)
            || self.evidence_fd == Some(self.anchor_fd)
            || !valid_hex(
                &self.controller_closure_sha256,
                MAX_CONTROLLER_CLOSURE_BYTES,
            )
            || !valid_hex(&self.runtime_identity_digest, MAX_RUNTIME_DIGEST_BYTES)
            || !valid_hex(&self.request_nonce, MAX_NONCE_BYTES)
        {
            return Err(ControllerError::MalformedRequest);
        }
        if self.profile != ControllerProfile::Rootless
            || self.operation != ControllerOperation::RequestShutdown
        {
            return Err(ControllerError::WrongDomain);
        }
        if compiled_controller_closure() == UNBOUND_CONTROLLER_CLOSURE
            || self.controller_closure_sha256 != compiled_controller_closure()
        {
            return Err(ControllerError::ClosureMismatch);
        }
        self.budget.validate()?;
        Ok(())
    }

    fn runtime_digest_bytes(&self) -> [u8; 32] {
        let mut result = [0; 32];
        for (slot, pair) in self
            .runtime_identity_digest
            .as_bytes()
            .chunks_exact(2)
            .enumerate()
        {
            result[slot] = (pair[0] as char).to_digit(16).unwrap() as u8 * 16
                + (pair[1] as char).to_digit(16).unwrap() as u8;
        }
        result
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SignalKind {
    Kill,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SignalResult {
    Sent,
    Gone,
    Rejected,
    Deadline,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum SignalTarget {
    SessionRoot,
    SessionMember,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum SignalEvidence {
    RustPidfd {
        #[serde(skip_deserializing)]
        target: SignalTarget,
        #[serde(skip_deserializing)]
        signal: SignalKind,
        #[serde(skip_deserializing)]
        result: SignalResult,
    },
    ProductProtocol {
        #[serde(skip_deserializing)]
        target: SignalTarget,
        #[serde(skip_deserializing)]
        signal: SignalKind,
        #[serde(skip_deserializing)]
        result: SignalResult,
        #[serde(skip_deserializing)]
        protocol: ProductProtocol,
        #[serde(skip_deserializing)]
        proof_digest: [u8; 32],
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ProductProtocol {
    DarlingShutdownV1,
}

impl SignalEvidence {
    fn rust_pidfd(target: SignalTarget, result: SignalResult) -> Self {
        Self::RustPidfd {
            target,
            signal: SignalKind::Kill,
            result,
        }
    }

    fn product_protocol(
        target: SignalTarget,
        result: SignalResult,
        proof_digest: [u8; 32],
    ) -> Self {
        Self::ProductProtocol {
            target,
            signal: SignalKind::Kill,
            result,
            protocol: ProductProtocol::DarlingShutdownV1,
            proof_digest,
        }
    }

    fn validate_for(&self, request: &ControllerRequest) -> Result<(), ControllerError> {
        match self {
            Self::RustPidfd { signal, .. } if *signal == SignalKind::Kill => Ok(()),
            Self::ProductProtocol {
                signal,
                protocol: ProductProtocol::DarlingShutdownV1,
                proof_digest,
                ..
            } if *signal == SignalKind::Kill && *proof_digest == request.runtime_digest_bytes() => {
                Ok(())
            }
            _ => Err(ControllerError::SignalEvidenceRequired),
        }
    }

    fn target(&self) -> SignalTarget {
        match self {
            Self::RustPidfd { target, .. } | Self::ProductProtocol { target, .. } => *target,
        }
    }

    fn result(&self) -> SignalResult {
        match self {
            Self::RustPidfd { result, .. } | Self::ProductProtocol { result, .. } => *result,
        }
    }

    fn reduce_for_shutdown(
        &self,
        request: &ControllerRequest,
        expected_target: SignalTarget,
    ) -> Result<(), ControllerError> {
        self.validate_for(request)?;
        if self.target() != expected_target {
            return Err(ControllerError::SignalEvidenceRequired);
        }
        match self.result() {
            SignalResult::Sent => Ok(()),
            SignalResult::Gone => Err(ControllerError::IdentityMismatch(
                RecoveryObligation::MembershipChanged,
            )),
            SignalResult::Rejected => Err(ControllerError::PartialSignal),
            SignalResult::Deadline => Err(ControllerError::BudgetExceeded),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "SCREAMING_SNAKE_CASE")]
pub(crate) enum JournalEvent {
    Prepared,
    CapabilitiesAcquired,
    MembershipBound {
        member_count: usize,
    },
    ShutdownRequested {
        signals: Vec<SignalEvidence>,
    },
    Drained,
    Quiescent,
    Cleaned {
        endpoint_count: usize,
    },
    Finalized {
        verdict: ControllerVerdict,
    },
    Recovery {
        obligation: RecoveryObligation,
        completed: usize,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ControllerVerdict {
    Success,
    FailClosed,
    Unsupported,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum RecoveryObligation {
    MarkerIdentity,
    MembershipChanged,
    LateFork,
    PidReuse,
    EndpointReplacement,
    LauncherIdentity,
    SignalEvidence,
    QuiescenceRequired,
    BudgetExceeded,
    ControllerInterrupted,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ControllerResponse {
    pub schema_version: u32,
    pub transaction_id: String,
    pub controller_closure_sha256: String,
    pub runtime_identity_digest: String,
    pub request_nonce: String,
    pub verdict: ControllerVerdict,
    pub obligations: Vec<RecoveryObligation>,
    pub signals: Vec<SignalEvidence>,
    pub journal: Vec<JournalEvent>,
}

#[derive(Debug, Eq, PartialEq)]
pub enum ControllerError {
    MalformedRequest,
    WrongDomain,
    ClosureMismatch,
    BudgetExceeded,
    IdentityMismatch(RecoveryObligation),
    MembershipChanged,
    MissingPidfd,
    SignalEvidenceRequired,
    QuiescenceRequired,
    EndpointReplacement,
    PartialSignal,
    PartialCleanup,
    Interrupted,
    UnresolvedObligation,
}

impl fmt::Display for ControllerError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{self:?}")
    }
}

impl std::error::Error for ControllerError {}

fn signal_obligation(error: &ControllerError) -> RecoveryObligation {
    match error {
        ControllerError::IdentityMismatch(obligation) => *obligation,
        ControllerError::BudgetExceeded => RecoveryObligation::BudgetExceeded,
        ControllerError::Interrupted => RecoveryObligation::ControllerInterrupted,
        _ => RecoveryObligation::SignalEvidence,
    }
}

fn push_obligation(obligations: &mut Vec<RecoveryObligation>, obligation: RecoveryObligation) {
    if !obligations.contains(&obligation) {
        obligations.push(obligation);
    }
}

fn finish_recovery_budget(
    budget: &mut BudgetLedger,
    error: &mut ControllerError,
    obligations: &mut Vec<RecoveryObligation>,
) -> bool {
    if budget.recovery().is_err() {
        *error = ControllerError::BudgetExceeded;
        push_obligation(obligations, RecoveryObligation::BudgetExceeded);
        true
    } else {
        false
    }
}

/// Every budget is consumed by a phase, not merely checked at request parse.
#[derive(Clone, Copy, Debug)]
struct BudgetLedger {
    limits: ControllerBudget,
    events: u32,
    virtual_time_ns: u64,
    members: u32,
    recovery_steps: u32,
    now_ns: u64,
}

impl BudgetLedger {
    fn new(limits: ControllerBudget) -> Self {
        Self {
            limits,
            events: 0,
            virtual_time_ns: 0,
            members: 0,
            recovery_steps: 0,
            now_ns: 0,
        }
    }

    fn event(&mut self) -> Result<(), ControllerError> {
        self.events = self.events.saturating_add(1);
        if self.events > self.limits.max_events || self.now_ns > self.limits.deadline_ns {
            Err(ControllerError::BudgetExceeded)
        } else {
            Ok(())
        }
    }

    fn members(&mut self, count: usize) -> Result<(), ControllerError> {
        self.members = self.members.saturating_add(count as u32);
        if self.members > self.limits.max_members {
            Err(ControllerError::BudgetExceeded)
        } else {
            Ok(())
        }
    }

    fn advance(&mut self, amount: u64) -> Result<(), ControllerError> {
        self.virtual_time_ns = self.virtual_time_ns.saturating_add(amount);
        self.now_ns = self.now_ns.saturating_add(amount);
        if self.virtual_time_ns > self.limits.max_virtual_time_ns
            || self.now_ns > self.limits.deadline_ns
        {
            Err(ControllerError::BudgetExceeded)
        } else {
            Ok(())
        }
    }

    fn recovery(&mut self) -> Result<(), ControllerError> {
        self.recovery_steps = self.recovery_steps.saturating_add(1);
        if self.recovery_steps > self.limits.max_recovery_steps {
            Err(ControllerError::BudgetExceeded)
        } else {
            Ok(())
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub struct ProcessIdentity {
    pub pid: i32,
    pub starttime: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct MarkerObservation {
    pub identity: FileIdentity,
    pub content_digest: [u8; 32],
    pub mode: u32,
    pub uid: u32,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct LauncherObservation {
    pub identity: FileIdentity,
    pub content_digest: [u8; 32],
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd, Serialize)]
pub enum EndpointKind {
    InitPid,
    DarlingServer,
    Shellspawn,
    Launchd,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub struct EndpointIdentity {
    pub kind: EndpointKind,
    pub identity: FileIdentity,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize)]
pub struct EndpointSnapshot {
    entries: BTreeMap<EndpointKind, FileIdentity>,
}

impl EndpointSnapshot {
    pub fn from_entries(
        entries: impl IntoIterator<Item = EndpointIdentity>,
    ) -> Result<Self, ControllerError> {
        let mut result = Self::default();
        for entry in entries {
            if result.entries.insert(entry.kind, entry.identity).is_some()
                || result.entries.len() > MAX_ENDPOINTS
            {
                return Err(ControllerError::EndpointReplacement);
            }
        }
        Ok(result)
    }
    pub fn len(&self) -> usize {
        self.entries.len()
    }
    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
pub enum MembershipSource {
    RustProcTaskChildren,
    ProductProtocol,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct MembershipSnapshot {
    pub source: MembershipSource,
    pub complete: bool,
    pub root: Option<ProcessIdentity>,
    pub members: Vec<ProcessIdentity>,
}

impl MembershipSnapshot {
    pub fn empty(source: MembershipSource) -> Self {
        Self {
            source,
            complete: true,
            root: None,
            members: Vec::new(),
        }
    }
    fn validate(&self) -> Result<(), ControllerError> {
        if !self.complete || self.members.len() > MAX_MEMBERS {
            return Err(ControllerError::MembershipChanged);
        }
        let mut identities = self.members.clone();
        identities.sort_unstable();
        identities.dedup();
        if identities.len() != self.members.len() {
            return Err(ControllerError::MembershipChanged);
        }
        Ok(())
    }
}

/// Non-clone owning capabilities.  The constructors below are private or
/// test-only; callers receive only an inherited anchor descriptor.
pub struct PrefixCapability {
    fd: OwnedFd,
    identity: FileIdentity,
    scope: ScopeId,
}
#[derive(Debug)]
pub struct MarkerCapability {
    fd: OwnedFd,
    observation: MarkerObservation,
    scope: ScopeId,
}
#[derive(Debug)]
pub struct LauncherCapability {
    fd: OwnedFd,
    observation: LauncherObservation,
    scope: ScopeId,
}
#[derive(Debug)]
pub struct PidFdCapability {
    fd: OwnedFd,
    identity: ProcessIdentity,
    target: SignalTarget,
    scope: ScopeId,
}
#[derive(Debug)]
pub struct EndpointCapability {
    fd: OwnedFd,
    kind: EndpointKind,
    identity: FileIdentity,
    scope: ScopeId,
}

impl fmt::Debug for PrefixCapability {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("PrefixCapability")
            .field("identity", &self.identity)
            .finish()
    }
}
impl PrefixCapability {
    pub fn identity(&self) -> FileIdentity {
        self.identity
    }
    fn scope(&self) -> ScopeId {
        self.scope
    }
    fn raw_fd(&self) -> RawFd {
        self.fd.as_raw_fd()
    }
    fn revalidate(&self, observed: FileIdentity) -> Result<(), ControllerError> {
        if observed == self.identity {
            Ok(())
        } else {
            Err(ControllerError::IdentityMismatch(
                RecoveryObligation::MembershipChanged,
            ))
        }
    }
}
impl MarkerCapability {
    pub fn observation(&self) -> MarkerObservation {
        self.observation
    }
    fn scope(&self) -> ScopeId {
        self.scope
    }
    fn revalidate(&self, observed: MarkerObservation) -> Result<(), ControllerError> {
        if observed == self.observation {
            Ok(())
        } else {
            Err(ControllerError::IdentityMismatch(
                RecoveryObligation::MarkerIdentity,
            ))
        }
    }
}
impl LauncherCapability {
    pub fn observation(&self) -> LauncherObservation {
        self.observation
    }
    fn scope(&self) -> ScopeId {
        self.scope
    }
    fn revalidate(&self, observed: LauncherObservation) -> Result<(), ControllerError> {
        if observed == self.observation {
            Ok(())
        } else {
            Err(ControllerError::IdentityMismatch(
                RecoveryObligation::EndpointReplacement,
            ))
        }
    }
}
impl PidFdCapability {
    pub fn identity(&self) -> ProcessIdentity {
        self.identity
    }
    pub fn target(&self) -> SignalTarget {
        self.target
    }
    fn scope(&self) -> ScopeId {
        self.scope
    }
    fn revalidate(&self, observed: ProcessIdentity) -> Result<(), ControllerError> {
        if observed == self.identity {
            Ok(())
        } else {
            Err(ControllerError::IdentityMismatch(
                RecoveryObligation::PidReuse,
            ))
        }
    }
}
impl EndpointCapability {
    fn scope(&self) -> ScopeId {
        self.scope
    }
    fn identity(&self) -> EndpointIdentity {
        EndpointIdentity {
            kind: self.kind,
            identity: self.identity,
        }
    }
    fn revalidate(&self, observed: FileIdentity) -> Result<(), ControllerError> {
        if observed == self.identity {
            Ok(())
        } else {
            Err(ControllerError::EndpointReplacement)
        }
    }
}

pub struct AnchorCapabilities {
    anchor_fd: OwnedFd,
    evidence_fd: Option<OwnedFd>,
    anchor_identity: FileIdentity,
    scope: ScopeId,
}
impl AnchorCapabilities {
    /// The future controller binary calls this only after validating inherited
    /// descriptor ownership.  No path or launcher descriptor crosses this
    /// boundary.
    pub(crate) fn from_inherited(
        anchor_fd: OwnedFd,
        evidence_fd: Option<OwnedFd>,
    ) -> Result<Self, ControllerError> {
        let anchor_identity = FileIdentity::from_fd(anchor_fd.as_raw_fd())
            .map_err(|_| ControllerError::MalformedRequest)?;
        if anchor_identity.mode & libc::S_IFMT != libc::S_IFDIR {
            return Err(ControllerError::MalformedRequest);
        }
        if let Some(evidence) = evidence_fd.as_ref() {
            let evidence_identity = FileIdentity::from_fd(evidence.as_raw_fd())
                .map_err(|_| ControllerError::MalformedRequest)?;
            if evidence_identity.mode & libc::S_IFMT != libc::S_IFREG {
                return Err(ControllerError::MalformedRequest);
            }
        }
        Ok(Self {
            anchor_fd,
            evidence_fd,
            anchor_identity,
            scope: fresh_scope(),
        })
    }

    #[cfg(test)]
    fn test_owned(anchor_fd: OwnedFd, evidence_fd: Option<OwnedFd>) -> Self {
        Self {
            anchor_fd,
            evidence_fd,
            anchor_identity: FileIdentity {
                device: 1,
                inode: 2,
                mode: 0o40700,
                nlink: 1,
                uid: 1000,
                gid: 1000,
            },
            scope: fresh_scope(),
        }
    }
}

mod sealed {
    pub trait CapabilityInspector {}
    pub trait SignalExecutor {}
    pub trait QuiescenceBackend {}
    pub trait EndpointCleanupExecutor {}
}

/// Sealed Rust acquisition seam.  Its implementation receives the anchor and
/// performs all fd-relative opens/identity reads internally; Python cannot
/// implement this trait or provide a launcher/pidfd bundle.
pub(crate) trait CapabilityInspector: sealed::CapabilityInspector {
    fn acquire_from_anchor(
        &mut self,
        anchor: AnchorCapabilities,
        max_members: usize,
    ) -> Result<AcquisitionBundle, ControllerError>;
}

#[derive(Debug)]
pub struct AcquisitionBundle {
    scope: ScopeId,
    evidence: Option<OwnedFd>,
    prefix: PrefixCapability,
    marker: MarkerCapability,
    launcher: LauncherCapability,
    root: PidFdCapability,
    members: Vec<PidFdCapability>,
    endpoints: Vec<EndpointCapability>,
}

impl AcquisitionBundle {
    #[cfg(test)]
    #[allow(clippy::too_many_arguments)]
    fn from_owned_for_test(
        anchor: AnchorCapabilities,
        marker_fd: OwnedFd,
        marker: MarkerObservation,
        launch_fd: OwnedFd,
        launcher: LauncherObservation,
        root_fd: OwnedFd,
        root_identity: ProcessIdentity,
        member_fds: Vec<(OwnedFd, ProcessIdentity)>,
        endpoint: OwnedFd,
    ) -> Result<Self, ControllerError> {
        if member_fds.len() > MAX_MEMBERS {
            return Err(ControllerError::BudgetExceeded);
        }
        let AnchorCapabilities {
            anchor_fd,
            evidence_fd,
            anchor_identity,
            scope,
        } = anchor;
        let prefix = PrefixCapability {
            fd: anchor_fd,
            identity: anchor_identity,
            scope,
        };
        let members = member_fds
            .into_iter()
            .map(|(fd, identity)| PidFdCapability {
                fd,
                identity,
                target: SignalTarget::SessionMember,
                scope,
            })
            .collect();
        Ok(Self {
            scope,
            evidence: evidence_fd,
            prefix,
            marker: MarkerCapability {
                fd: marker_fd,
                observation: marker,
                scope,
            },
            launcher: LauncherCapability {
                fd: launch_fd,
                observation: launcher,
                scope,
            },
            root: PidFdCapability {
                fd: root_fd,
                identity: root_identity,
                target: SignalTarget::SessionRoot,
                scope,
            },
            members,
            endpoints: vec![EndpointCapability {
                fd: endpoint,
                kind: EndpointKind::Shellspawn,
                identity: FileIdentity {
                    device: 4,
                    inode: 5,
                    mode: 0o140600,
                    nlink: 1,
                    uid: 1000,
                    gid: 1000,
                },
                scope,
            }],
        })
    }
    fn validate_scope(&self) -> Result<(), ControllerError> {
        if self.scope != self.prefix.scope()
            || self.scope != self.marker.scope()
            || self.scope != self.launcher.scope()
            || self.scope != self.root.scope()
            || self.members.iter().any(|cap| cap.scope() != self.scope)
            || self.endpoints.iter().any(|cap| cap.scope() != self.scope)
        {
            Err(ControllerError::MissingPidfd)
        } else {
            Ok(())
        }
    }
    pub fn member_count(&self) -> usize {
        self.members.len()
    }
}

pub struct Controller;
#[derive(Debug)]
pub struct Prepared {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    budget: BudgetLedger,
}
#[derive(Debug)]
pub struct Acquired {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    budget: BudgetLedger,
    capabilities: AcquisitionBundle,
}
#[derive(Debug)]
pub struct MembershipBound {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    budget: BudgetLedger,
    capabilities: AcquisitionBundle,
    initial: MembershipSnapshot,
    pending_signals: Vec<SignalEvidence>,
}
#[derive(Debug)]
pub struct ShutdownRequested {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    budget: BudgetLedger,
    capabilities: AcquisitionBundle,
    initial: MembershipSnapshot,
    signals: Vec<SignalEvidence>,
}
#[derive(Debug)]
pub struct Drained {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    budget: BudgetLedger,
    capabilities: AcquisitionBundle,
    signals: Vec<SignalEvidence>,
}
#[derive(Debug)]
pub struct QuiescenceProof {
    scope: ScopeId,
    nonce: u64,
}
#[derive(Debug)]
pub struct Quiescent {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    budget: BudgetLedger,
    capabilities: AcquisitionBundle,
    signals: Vec<SignalEvidence>,
    proof: QuiescenceProof,
}
#[derive(Debug)]
pub struct Cleaned {
    request: ControllerRequest,
    journal: Vec<JournalEvent>,
    signals: Vec<SignalEvidence>,
    endpoint_count: usize,
}
pub struct Finalized {
    response: ControllerResponse,
}

#[derive(Debug)]
pub struct RecoveryPending<S> {
    state: S,
    error: ControllerError,
    obligations: Vec<RecoveryObligation>,
}

/// The state is consumed when a recovery result is emitted.  Capabilities are
/// deliberately dropped here; no placeholder descriptors or fresh
/// allocations are needed to turn a partially-mutated transaction into a
/// typed terminal response.
trait RecoveryStateParts {
    fn journal(&self) -> &[JournalEvent];
    fn into_response_parts(self) -> (ControllerRequest, Vec<JournalEvent>, Vec<SignalEvidence>);
}

impl RecoveryStateParts for Acquired {
    fn journal(&self) -> &[JournalEvent] {
        &self.journal
    }

    fn into_response_parts(self) -> (ControllerRequest, Vec<JournalEvent>, Vec<SignalEvidence>) {
        let Self {
            request, journal, ..
        } = self;
        (request, journal, Vec::new())
    }
}

impl RecoveryStateParts for MembershipBound {
    fn journal(&self) -> &[JournalEvent] {
        &self.journal
    }

    fn into_response_parts(self) -> (ControllerRequest, Vec<JournalEvent>, Vec<SignalEvidence>) {
        let Self {
            request,
            journal,
            pending_signals,
            ..
        } = self;
        (request, journal, pending_signals)
    }
}

impl RecoveryStateParts for ShutdownRequested {
    fn journal(&self) -> &[JournalEvent] {
        &self.journal
    }

    fn into_response_parts(self) -> (ControllerRequest, Vec<JournalEvent>, Vec<SignalEvidence>) {
        let Self {
            request,
            journal,
            signals,
            ..
        } = self;
        (request, journal, signals)
    }
}

impl RecoveryStateParts for Drained {
    fn journal(&self) -> &[JournalEvent] {
        &self.journal
    }

    fn into_response_parts(self) -> (ControllerRequest, Vec<JournalEvent>, Vec<SignalEvidence>) {
        let Self {
            request,
            journal,
            signals,
            ..
        } = self;
        (request, journal, signals)
    }
}

impl RecoveryStateParts for Quiescent {
    fn journal(&self) -> &[JournalEvent] {
        &self.journal
    }

    fn into_response_parts(self) -> (ControllerRequest, Vec<JournalEvent>, Vec<SignalEvidence>) {
        let Self {
            request,
            journal,
            signals,
            ..
        } = self;
        (request, journal, signals)
    }
}

impl<S> RecoveryPending<S> {
    pub fn error(&self) -> &ControllerError {
        &self.error
    }
    pub fn obligations(&self) -> &[RecoveryObligation] {
        &self.obligations
    }
}

#[allow(private_bounds)]
impl<S: RecoveryStateParts> RecoveryPending<S> {
    pub(crate) fn journal(&self) -> &[JournalEvent] {
        self.state.journal()
    }
}

#[allow(private_bounds)]
impl<S: RecoveryStateParts> RecoveryPending<S> {
    /// Consume a failed transaction into its only v1 terminal response.
    /// Recovery is deliberately not resumable in this version: retained
    /// capabilities and obligations are reported, then the transaction fails
    /// closed without attempting a second mutation.
    pub(crate) fn finalize_fail_closed(self) -> Finalized {
        let Self {
            state, obligations, ..
        } = self;
        let (request, mut journal, signals) = state.into_response_parts();
        let verdict = ControllerVerdict::FailClosed;
        journal.push(JournalEvent::Finalized { verdict });
        Finalized {
            response: ControllerResponse {
                schema_version: CONTROLLER_SCHEMA_VERSION,
                transaction_id: request.transaction_id,
                controller_closure_sha256: request.controller_closure_sha256,
                runtime_identity_digest: request.runtime_identity_digest,
                request_nonce: request.request_nonce,
                verdict,
                obligations,
                signals,
                journal,
            },
        }
    }
}

pub(crate) trait SignalExecutor: sealed::SignalExecutor {
    fn signal_member(
        &mut self,
        capability: &PidFdCapability,
    ) -> Result<SignalEvidence, ControllerError>;
    fn signal_root(
        &mut self,
        capability: &PidFdCapability,
    ) -> Result<SignalEvidence, ControllerError>;
}

pub struct CleanupFailure {
    pub removed: usize,
    pub obligation: RecoveryObligation,
}
pub(crate) trait EndpointCleanupExecutor: sealed::EndpointCleanupExecutor {
    /// The implementation receives retained endpoint FDs.  It must revalidate
    /// each FD immediately before the unlink/rename syscall; snapshots are not
    /// accepted as authority.
    fn cleanup_fd_relative(
        &mut self,
        prefix: &PrefixCapability,
        endpoints: &mut [EndpointCapability],
        proof: &QuiescenceProof,
    ) -> Result<usize, CleanupFailure>;
}

pub(crate) trait QuiescenceBackend: sealed::QuiescenceBackend {
    fn prove(&mut self, prefix: &PrefixCapability) -> Result<QuiescenceProof, ControllerError>;
}

impl Controller {
    pub fn prepare(request: ControllerRequest) -> Result<Prepared, ControllerError> {
        request.validate()?;
        Ok(Prepared {
            budget: BudgetLedger::new(request.budget),
            request,
            journal: vec![JournalEvent::Prepared],
        })
    }
}

impl Prepared {
    pub(crate) fn acquire(
        self,
        anchor: AnchorCapabilities,
        inspector: &mut impl CapabilityInspector,
    ) -> Result<Acquired, ControllerError> {
        if anchor.anchor_fd.as_raw_fd() != self.request.anchor_fd
            || anchor.evidence_fd.as_ref().map(AsRawFd::as_raw_fd) != self.request.evidence_fd
        {
            return Err(ControllerError::MalformedRequest);
        }
        let mut budget = self.budget;
        budget.event()?;
        budget.advance(1)?;
        let capabilities =
            inspector.acquire_from_anchor(anchor, self.request.budget.max_members as usize)?;
        capabilities.validate_scope()?;
        if capabilities.launcher.observation.content_digest != self.request.runtime_digest_bytes() {
            return Err(ControllerError::IdentityMismatch(
                RecoveryObligation::LauncherIdentity,
            ));
        }
        let mut journal = self.journal;
        journal.push(JournalEvent::CapabilitiesAcquired);
        Ok(Acquired {
            request: self.request,
            journal,
            budget,
            capabilities,
        })
    }
}

impl Acquired {
    pub(crate) fn bind_membership(
        mut self,
        snapshot: MembershipSnapshot,
    ) -> Result<MembershipBound, RecoveryPending<Acquired>> {
        if let Err(error) = self
            .budget
            .event()
            .and_then(|_| self.budget.advance(1))
            .and_then(|_| snapshot.validate())
            .and_then(|_| self.budget.members(snapshot.members.len()))
        {
            return Err(self.recovery(error, RecoveryObligation::MembershipChanged));
        }
        if snapshot.root != Some(self.capabilities.root.identity)
            || snapshot.members.len() != self.capabilities.members.len()
            || snapshot.members.iter().any(|identity| {
                !self
                    .capabilities
                    .members
                    .iter()
                    .any(|member| member.identity == *identity)
            })
        {
            return Err(self.recovery(
                ControllerError::MembershipChanged,
                RecoveryObligation::MembershipChanged,
            ));
        }
        self.journal.push(JournalEvent::MembershipBound {
            member_count: snapshot.members.len(),
        });
        Ok(MembershipBound {
            request: self.request,
            journal: self.journal,
            budget: self.budget,
            capabilities: self.capabilities,
            initial: snapshot,
            pending_signals: Vec::new(),
        })
    }
    fn recovery(
        self,
        mut error: ControllerError,
        obligation: RecoveryObligation,
    ) -> RecoveryPending<Acquired> {
        let Self {
            request,
            mut journal,
            mut budget,
            capabilities,
        } = self;
        let mut obligations = vec![obligation];
        let recovery_budget_failed =
            finish_recovery_budget(&mut budget, &mut error, &mut obligations);
        journal.push(JournalEvent::Recovery {
            obligation,
            completed: 0,
        });
        if recovery_budget_failed {
            journal.push(JournalEvent::Recovery {
                obligation: RecoveryObligation::BudgetExceeded,
                completed: 0,
            });
        }
        RecoveryPending {
            state: Self {
                request,
                journal,
                budget,
                capabilities,
            },
            error,
            obligations,
        }
    }
}

impl MembershipBound {
    fn verify_before_shutdown(&self, snapshot: &MembershipSnapshot) -> Result<(), ControllerError> {
        snapshot.validate()?;
        if snapshot != &self.initial {
            if snapshot.members.len() > self.initial.members.len() {
                return Err(ControllerError::IdentityMismatch(
                    RecoveryObligation::LateFork,
                ));
            }
            if snapshot.members.iter().any(|candidate| {
                self.initial.members.iter().any(|known| {
                    known.pid == candidate.pid && known.starttime != candidate.starttime
                })
            }) {
                return Err(ControllerError::IdentityMismatch(
                    RecoveryObligation::PidReuse,
                ));
            }
            return Err(ControllerError::MembershipChanged);
        }
        Ok(())
    }

    pub(crate) fn request_shutdown(
        mut self,
        current: MembershipSnapshot,
        executor: &mut impl SignalExecutor,
    ) -> Result<ShutdownRequested, RecoveryPending<MembershipBound>> {
        if let Err(error) = self.verify_before_shutdown(&current) {
            let obligation = match &error {
                ControllerError::IdentityMismatch(value) => *value,
                _ => RecoveryObligation::MembershipChanged,
            };
            return Err(self.recovery(error, obligation));
        }
        let mut signals = Vec::with_capacity(self.capabilities.members.len() + 1);
        for member in &self.capabilities.members {
            if let Err(error) = self.budget.event().and_then(|_| self.budget.advance(1)) {
                return Err(self.recovery(error, RecoveryObligation::BudgetExceeded));
            }
            let evidence = match executor.signal_member(member) {
                Ok(value) => value,
                Err(error) => {
                    let obligation = signal_obligation(&error);
                    return Err(self.recovery(error, obligation));
                }
            };
            if let Err(error) =
                evidence.reduce_for_shutdown(&self.request, SignalTarget::SessionMember)
            {
                let obligation = signal_obligation(&error);
                return Err(self.recovery(error, obligation));
            }
            signals.push(evidence);
            self.pending_signals = signals.clone();
        }
        if let Err(error) = self.budget.event().and_then(|_| self.budget.advance(1)) {
            return Err(self.recovery(error, RecoveryObligation::BudgetExceeded));
        }
        let root = match executor.signal_root(&self.capabilities.root) {
            Ok(value) => value,
            Err(error) => {
                let obligation = signal_obligation(&error);
                return Err(self.recovery(error, obligation));
            }
        };
        if let Err(error) = root.reduce_for_shutdown(&self.request, SignalTarget::SessionRoot) {
            let obligation = signal_obligation(&error);
            return Err(self.recovery(error, obligation));
        }
        signals.push(root);
        self.journal.push(JournalEvent::ShutdownRequested {
            signals: signals.clone(),
        });
        Ok(ShutdownRequested {
            request: self.request,
            journal: self.journal,
            budget: self.budget,
            capabilities: self.capabilities,
            initial: self.initial,
            signals,
        })
    }
    fn recovery(
        self,
        mut error: ControllerError,
        obligation: RecoveryObligation,
    ) -> RecoveryPending<MembershipBound> {
        let Self {
            request,
            mut journal,
            mut budget,
            capabilities,
            initial,
            pending_signals,
        } = self;
        let mut obligations = vec![obligation];
        let recovery_budget_failed =
            finish_recovery_budget(&mut budget, &mut error, &mut obligations);
        journal.push(JournalEvent::Recovery {
            obligation,
            completed: pending_signals.len(),
        });
        if recovery_budget_failed {
            journal.push(JournalEvent::Recovery {
                obligation: RecoveryObligation::BudgetExceeded,
                completed: pending_signals.len(),
            });
        }
        RecoveryPending {
            state: MembershipBound {
                request,
                journal,
                budget,
                capabilities,
                initial,
                pending_signals,
            },
            error,
            obligations,
        }
    }
}

impl ShutdownRequested {
    pub(crate) fn drain(
        mut self,
        final_membership: MembershipSnapshot,
    ) -> Result<Drained, RecoveryPending<ShutdownRequested>> {
        if let Err(error) = self
            .budget
            .event()
            .and_then(|_| self.budget.advance(1))
            .and_then(|_| final_membership.validate())
            .and_then(|_| {
                if final_membership.root.is_none() && final_membership.members.is_empty() {
                    Ok(())
                } else {
                    Err(ControllerError::MembershipChanged)
                }
            })
        {
            return Err(self.recovery(error, RecoveryObligation::MembershipChanged));
        }
        self.journal.push(JournalEvent::Drained);
        Ok(Drained {
            request: self.request,
            journal: self.journal,
            budget: self.budget,
            capabilities: self.capabilities,
            signals: self.signals,
        })
    }
    fn recovery(
        self,
        mut error: ControllerError,
        obligation: RecoveryObligation,
    ) -> RecoveryPending<ShutdownRequested> {
        let Self {
            request,
            mut journal,
            mut budget,
            capabilities,
            initial,
            signals,
        } = self;
        let mut obligations = vec![obligation];
        let recovery_budget_failed =
            finish_recovery_budget(&mut budget, &mut error, &mut obligations);
        journal.push(JournalEvent::Recovery {
            obligation,
            completed: signals.len(),
        });
        if recovery_budget_failed {
            journal.push(JournalEvent::Recovery {
                obligation: RecoveryObligation::BudgetExceeded,
                completed: signals.len(),
            });
        }
        RecoveryPending {
            state: ShutdownRequested {
                request,
                journal,
                budget,
                capabilities,
                initial,
                signals,
            },
            error,
            obligations,
        }
    }
}

impl Drained {
    pub(crate) fn establish_quiescence(
        mut self,
        backend: &mut impl QuiescenceBackend,
    ) -> Result<Quiescent, RecoveryPending<Drained>> {
        if let Err(error) = self.budget.event().and_then(|_| self.budget.advance(1)) {
            return Err(self.recovery(error, RecoveryObligation::BudgetExceeded));
        }
        let proof = match backend.prove(&self.capabilities.prefix) {
            Ok(value) => value,
            Err(error) => return Err(self.recovery(error, RecoveryObligation::QuiescenceRequired)),
        };
        if proof.scope != self.capabilities.prefix.scope() || proof.nonce == 0 {
            return Err(self.recovery(
                ControllerError::QuiescenceRequired,
                RecoveryObligation::QuiescenceRequired,
            ));
        }
        self.journal.push(JournalEvent::Quiescent);
        Ok(Quiescent {
            request: self.request,
            journal: self.journal,
            budget: self.budget,
            capabilities: self.capabilities,
            signals: self.signals,
            proof,
        })
    }
    fn recovery(
        self,
        mut error: ControllerError,
        obligation: RecoveryObligation,
    ) -> RecoveryPending<Drained> {
        let Self {
            request,
            mut journal,
            mut budget,
            capabilities,
            signals,
        } = self;
        let mut obligations = vec![obligation];
        let recovery_budget_failed =
            finish_recovery_budget(&mut budget, &mut error, &mut obligations);
        journal.push(JournalEvent::Recovery {
            obligation,
            completed: 0,
        });
        if recovery_budget_failed {
            journal.push(JournalEvent::Recovery {
                obligation: RecoveryObligation::BudgetExceeded,
                completed: 0,
            });
        }
        RecoveryPending {
            state: Drained {
                request,
                journal,
                budget,
                capabilities,
                signals,
            },
            error,
            obligations,
        }
    }
}

impl Quiescent {
    pub(crate) fn cleanup(
        mut self,
        executor: &mut impl EndpointCleanupExecutor,
    ) -> Result<Cleaned, RecoveryPending<Quiescent>> {
        if let Err(error) = self.budget.event().and_then(|_| self.budget.advance(1)) {
            return Err(self.recovery(error, RecoveryObligation::BudgetExceeded));
        }
        let endpoint_count = match executor.cleanup_fd_relative(
            &self.capabilities.prefix,
            &mut self.capabilities.endpoints,
            &self.proof,
        ) {
            Ok(count) => count,
            Err(failure) => {
                return Err(self.recovery_with_completed(
                    ControllerError::PartialCleanup,
                    failure.obligation,
                    failure.removed,
                ))
            }
        };
        self.journal.push(JournalEvent::Cleaned { endpoint_count });
        Ok(Cleaned {
            request: self.request,
            journal: self.journal,
            signals: self.signals,
            endpoint_count,
        })
    }
    fn recovery(
        self,
        error: ControllerError,
        obligation: RecoveryObligation,
    ) -> RecoveryPending<Quiescent> {
        self.recovery_with_completed(error, obligation, 0)
    }
    fn recovery_with_completed(
        self,
        mut error: ControllerError,
        obligation: RecoveryObligation,
        completed: usize,
    ) -> RecoveryPending<Quiescent> {
        let Self {
            request,
            mut journal,
            mut budget,
            capabilities,
            signals,
            proof,
        } = self;
        let mut obligations = vec![obligation];
        let recovery_budget_failed =
            finish_recovery_budget(&mut budget, &mut error, &mut obligations);
        journal.push(JournalEvent::Recovery {
            obligation,
            completed,
        });
        if recovery_budget_failed {
            journal.push(JournalEvent::Recovery {
                obligation: RecoveryObligation::BudgetExceeded,
                completed,
            });
        }
        RecoveryPending {
            state: Quiescent {
                request,
                journal,
                budget,
                capabilities,
                signals,
                proof,
            },
            error,
            obligations,
        }
    }
}

impl Cleaned {
    pub fn finalize(self) -> Result<Finalized, ControllerError> {
        let mut journal = self.journal;
        journal.push(JournalEvent::Finalized {
            verdict: ControllerVerdict::Success,
        });
        Ok(Finalized {
            response: ControllerResponse {
                schema_version: CONTROLLER_SCHEMA_VERSION,
                transaction_id: self.request.transaction_id,
                controller_closure_sha256: self.request.controller_closure_sha256,
                runtime_identity_digest: self.request.runtime_identity_digest,
                request_nonce: self.request.request_nonce,
                verdict: ControllerVerdict::Success,
                obligations: Vec::new(),
                signals: self.signals,
                journal,
            },
        })
    }
}
impl Finalized {
    pub(crate) fn response(self) -> ControllerResponse {
        self.response
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;
    use std::fs::File;

    #[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
    #[serde(rename_all = "kebab-case")]
    enum FixtureId {
        AncestorSwap,
        Deadline,
        DeployedLauncherReplacement,
        EndpointReplacementAfterCheck,
        LateFork,
        MalformedTransport,
        MarkerInPlace,
        MemberGone,
        MembershipDriftBeforeShutdown,
        MembershipDriftProvisioning,
        PidReuse,
        Sigint,
        StaleController,
    }

    #[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
    #[serde(rename_all = "lowercase")]
    enum FixtureClass {
        Identity,
        Budget,
        Launcher,
        Cleanup,
        Membership,
        Protocol,
        Marker,
        Interrupt,
        Provenance,
    }

    #[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq)]
    #[serde(rename_all = "SCREAMING_SNAKE_CASE")]
    enum FixtureExpected {
        MarkerIdentity,
        MembershipChanged,
        LateFork,
        PidReuse,
        EndpointReplacement,
        BudgetExceeded,
        ControllerInterrupted,
        MalformedRequest,
    }

    #[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
    #[serde(tag = "kind", deny_unknown_fields)]
    enum FixtureMutation {
        #[serde(rename = "ancestor-swap")]
        AncestorSwap {
            component: String,
            replacement: bool,
        },
        #[serde(rename = "deadline")]
        Deadline { phase: String, deadline: String },
        #[serde(rename = "deployed-launcher-replacement")]
        DeployedLauncherReplacement {
            verifier_closure: String,
            launcher: String,
            content_digest: String,
        },
        #[serde(rename = "endpoint-replacement-after-check")]
        EndpointReplacementAfterCheck {
            phase: String,
            same_name: bool,
            identity: String,
            operation: String,
        },
        #[serde(rename = "late-fork")]
        LateFork {
            phase: String,
            new_child: bool,
            observed_by_controller: bool,
        },
        #[serde(rename = "malformed-transport")]
        MalformedTransport { input: String, size: String },
        #[serde(rename = "marker-in-place")]
        MarkerInPlace {
            same_inode: bool,
            content_changed: bool,
            mode_changed: bool,
            owner_changed: bool,
        },
        #[serde(rename = "member-gone")]
        MemberGone {
            member: String,
            pidfd: String,
            signal: String,
        },
        #[serde(rename = "membership-drift-before-shutdown")]
        MembershipDriftBeforeShutdown {
            phase: String,
            child_set: String,
            pidfd_coverage: String,
        },
        #[serde(rename = "membership-drift-provisioning")]
        MembershipDriftProvisioning {
            phase: String,
            child_set: String,
            starttime: String,
        },
        #[serde(rename = "pid-reuse")]
        PidReuse {
            pid: String,
            starttime: String,
            pidfd: String,
        },
        #[serde(rename = "sigint")]
        Sigint { phase: String, rollback: String },
        #[serde(rename = "stale-controller")]
        StaleController {
            verifier_closure: String,
            controller_binary: String,
        },
    }

    #[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
    #[serde(deny_unknown_fields)]
    struct FixtureRecord {
        id: FixtureId,
        class: FixtureClass,
        mutation: FixtureMutation,
        expected: FixtureExpected,
    }

    fn fd() -> OwnedFd {
        File::open("/dev/null").unwrap().into()
    }
    fn identity(seed: u64) -> FileIdentity {
        FileIdentity {
            device: seed,
            inode: seed + 1,
            mode: 0o100600,
            nlink: 1,
            uid: 1000,
            gid: 1000,
        }
    }
    fn request(anchor_fd: RawFd) -> ControllerRequest {
        ControllerRequest {
            schema_version: CONTROLLER_SCHEMA_VERSION,
            transaction_id: "red-contract".into(),
            profile: ControllerProfile::Rootless,
            operation: ControllerOperation::RequestShutdown,
            anchor_fd,
            evidence_fd: None,
            controller_closure_sha256: compiled_controller_closure().into(),
            runtime_identity_digest: "03".repeat(32),
            request_nonce: "33".repeat(32),
            budget: ControllerBudget {
                max_events: 32,
                max_virtual_time_ns: 1000,
                max_members: 4,
                max_recovery_steps: 8,
                deadline_ns: 1000,
            },
        }
    }

    struct Inspector;
    impl sealed::CapabilityInspector for Inspector {}
    impl CapabilityInspector for Inspector {
        fn acquire_from_anchor(
            &mut self,
            anchor: AnchorCapabilities,
            _: usize,
        ) -> Result<AcquisitionBundle, ControllerError> {
            AcquisitionBundle::from_owned_for_test(
                anchor,
                fd(),
                MarkerObservation {
                    identity: identity(2),
                    content_digest: [2; 32],
                    mode: 0o100600,
                    uid: 1000,
                },
                fd(),
                LauncherObservation {
                    identity: identity(3),
                    content_digest: [3; 32],
                },
                fd(),
                ProcessIdentity {
                    pid: 10,
                    starttime: 20,
                },
                vec![(
                    fd(),
                    ProcessIdentity {
                        pid: 11,
                        starttime: 21,
                    },
                )],
                fd(),
            )
        }
    }
    struct Signals {
        fail_root: bool,
    }
    impl sealed::SignalExecutor for Signals {}
    impl SignalExecutor for Signals {
        fn signal_member(
            &mut self,
            _: &PidFdCapability,
        ) -> Result<SignalEvidence, ControllerError> {
            Ok(SignalEvidence::rust_pidfd(
                SignalTarget::SessionMember,
                SignalResult::Sent,
            ))
        }
        fn signal_root(&mut self, _: &PidFdCapability) -> Result<SignalEvidence, ControllerError> {
            if self.fail_root {
                Err(ControllerError::Interrupted)
            } else {
                Ok(SignalEvidence::rust_pidfd(
                    SignalTarget::SessionRoot,
                    SignalResult::Sent,
                ))
            }
        }
    }
    struct Quiescence;
    impl sealed::QuiescenceBackend for Quiescence {}
    impl QuiescenceBackend for Quiescence {
        fn prove(&mut self, prefix: &PrefixCapability) -> Result<QuiescenceProof, ControllerError> {
            Ok(QuiescenceProof {
                scope: prefix.scope(),
                nonce: 7,
            })
        }
    }
    struct Cleanup {
        replacement_after_verify: bool,
        verified: bool,
        replacement_preserved: bool,
        replacement_identity: Option<FileIdentity>,
        unlink_attempted: bool,
    }
    impl sealed::EndpointCleanupExecutor for Cleanup {}
    impl EndpointCleanupExecutor for Cleanup {
        fn cleanup_fd_relative(
            &mut self,
            prefix: &PrefixCapability,
            endpoints: &mut [EndpointCapability],
            proof: &QuiescenceProof,
        ) -> Result<usize, CleanupFailure> {
            assert_eq!(proof.scope, prefix.scope());
            let endpoint = endpoints
                .first()
                .expect("typed endpoint fixture has a retained capability");
            let original = endpoint.identity().identity;
            endpoint
                .revalidate(original)
                .expect("the retained endpoint must verify before the mutation hook");
            self.verified = true;
            if self.replacement_after_verify {
                // Model a same-name replacement after the successful verify and
                // before the final mutation syscall.  The executor must refuse
                // the unlink and leave the replacement untouched.
                self.replacement_identity = Some(identity(999));
                self.replacement_preserved = true;
                return endpoint
                    .revalidate(self.replacement_identity.expect("replacement identity"))
                    .map(|_| endpoints.len())
                    .map_err(|_| CleanupFailure {
                        removed: 0,
                        obligation: RecoveryObligation::EndpointReplacement,
                    });
            }
            self.unlink_attempted = true;
            Ok(endpoints.len())
        }
    }
    fn bound() -> MembershipBound {
        let anchor = AnchorCapabilities::test_owned(fd(), None);
        let prepared = Controller::prepare(request(anchor.anchor_fd.as_raw_fd())).unwrap();
        let mut inspector = Inspector;
        let acquired = prepared.acquire(anchor, &mut inspector).unwrap();
        acquired
            .bind_membership(MembershipSnapshot {
                source: MembershipSource::RustProcTaskChildren,
                complete: true,
                root: Some(ProcessIdentity {
                    pid: 10,
                    starttime: 20,
                }),
                members: vec![ProcessIdentity {
                    pid: 11,
                    starttime: 21,
                }],
            })
            .unwrap()
    }

    fn initial_snapshot() -> MembershipSnapshot {
        MembershipSnapshot {
            source: MembershipSource::RustProcTaskChildren,
            complete: true,
            root: Some(ProcessIdentity {
                pid: 10,
                starttime: 20,
            }),
            members: vec![ProcessIdentity {
                pid: 11,
                starttime: 21,
            }],
        }
    }

    fn quiescent_for_test() -> Quiescent {
        let bound = bound();
        let mut signals = Signals { fail_root: false };
        let requested = bound
            .request_shutdown(initial_snapshot(), &mut signals)
            .unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustProcTaskChildren,
            ))
            .unwrap();
        let mut q = Quiescence;
        drained.establish_quiescence(&mut q).unwrap()
    }

    #[test]
    fn red_marker_in_place_mutation_is_rejected() {
        let mut inspector = Inspector;
        let anchor = AnchorCapabilities::test_owned(fd(), None);
        let bundle = inspector.acquire_from_anchor(anchor, 4).unwrap();
        assert!(matches!(
            bundle.marker.revalidate(MarkerObservation {
                identity: identity(2),
                content_digest: [9; 32],
                mode: 0o100600,
                uid: 1000
            }),
            Err(ControllerError::IdentityMismatch(
                RecoveryObligation::MarkerIdentity
            ))
        ));
    }
    #[test]
    fn red_partial_signal_preserves_state_and_journal() {
        let bound = bound();
        let mut signals = Signals { fail_root: true };
        let pending = bound
            .request_shutdown(
                MembershipSnapshot {
                    source: MembershipSource::RustProcTaskChildren,
                    complete: true,
                    root: Some(ProcessIdentity {
                        pid: 10,
                        starttime: 20,
                    }),
                    members: vec![ProcessIdentity {
                        pid: 11,
                        starttime: 21,
                    }],
                },
                &mut signals,
            )
            .unwrap_err();
        assert_eq!(
            pending.obligations(),
            &[RecoveryObligation::ControllerInterrupted]
        );
        assert!(pending.journal().iter().any(|event| matches!(
            event,
            JournalEvent::Recovery {
                obligation: RecoveryObligation::ControllerInterrupted,
                completed: 1
            }
        )));
    }
    #[test]
    fn red_cleanup_after_last_verify_preserves_ownership() {
        let bound = bound();
        let mut signals = Signals { fail_root: false };
        let requested = bound
            .request_shutdown(
                MembershipSnapshot {
                    source: MembershipSource::RustProcTaskChildren,
                    complete: true,
                    root: Some(ProcessIdentity {
                        pid: 10,
                        starttime: 20,
                    }),
                    members: vec![ProcessIdentity {
                        pid: 11,
                        starttime: 21,
                    }],
                },
                &mut signals,
            )
            .unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustProcTaskChildren,
            ))
            .unwrap();
        let mut q = Quiescence;
        let quiescent = drained.establish_quiescence(&mut q).unwrap();
        let mut cleanup = Cleanup {
            replacement_after_verify: true,
            verified: false,
            replacement_preserved: false,
            replacement_identity: None,
            unlink_attempted: false,
        };
        let pending = quiescent.cleanup(&mut cleanup).unwrap_err();
        assert_eq!(
            pending.obligations(),
            &[RecoveryObligation::EndpointReplacement]
        );
        let response = pending.finalize_fail_closed().response;
        assert_eq!(response.verdict, ControllerVerdict::FailClosed);
        assert_eq!(
            response.obligations,
            vec![RecoveryObligation::EndpointReplacement]
        );
        assert!(cleanup.verified);
        assert!(cleanup.replacement_preserved);
        assert_eq!(cleanup.replacement_identity, Some(identity(999)));
        assert!(!cleanup.unlink_attempted);
    }
    #[test]
    fn red_success_is_not_recovery() {
        let bound = bound();
        let mut signals = Signals { fail_root: false };
        let requested = bound
            .request_shutdown(
                MembershipSnapshot {
                    source: MembershipSource::RustProcTaskChildren,
                    complete: true,
                    root: Some(ProcessIdentity {
                        pid: 10,
                        starttime: 20,
                    }),
                    members: vec![ProcessIdentity {
                        pid: 11,
                        starttime: 21,
                    }],
                },
                &mut signals,
            )
            .unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustProcTaskChildren,
            ))
            .unwrap();
        let mut q = Quiescence;
        let quiescent = drained.establish_quiescence(&mut q).unwrap();
        let mut cleanup = Cleanup {
            replacement_after_verify: false,
            verified: false,
            replacement_preserved: false,
            replacement_identity: None,
            unlink_attempted: false,
        };
        let response = quiescent
            .cleanup(&mut cleanup)
            .unwrap()
            .finalize()
            .unwrap()
            .response();
        assert_eq!(response.verdict, ControllerVerdict::Success);
        assert!(response.obligations.is_empty());
        assert!(cleanup.verified);
        assert!(cleanup.unlink_attempted);
    }

    #[test]
    fn red_closure_handshake_rejects_stale_controller() {
        assert_ne!(compiled_controller_closure(), UNBOUND_CONTROLLER_CLOSURE);
        let mut request = request(3);
        request.controller_closure_sha256 = "f".repeat(64);
        assert!(matches!(
            Controller::prepare(request),
            Err(ControllerError::ClosureMismatch)
        ));
    }

    #[test]
    fn red_signal_result_reducer_is_exhaustive_for_both_targets() {
        let request = request(3);
        for (target, result, expected) in [
            (
                SignalTarget::SessionMember,
                SignalResult::Gone,
                Err(ControllerError::IdentityMismatch(
                    RecoveryObligation::MembershipChanged,
                )),
            ),
            (
                SignalTarget::SessionMember,
                SignalResult::Rejected,
                Err(ControllerError::PartialSignal),
            ),
            (
                SignalTarget::SessionMember,
                SignalResult::Deadline,
                Err(ControllerError::BudgetExceeded),
            ),
            (
                SignalTarget::SessionRoot,
                SignalResult::Gone,
                Err(ControllerError::IdentityMismatch(
                    RecoveryObligation::MembershipChanged,
                )),
            ),
            (
                SignalTarget::SessionRoot,
                SignalResult::Rejected,
                Err(ControllerError::PartialSignal),
            ),
            (
                SignalTarget::SessionRoot,
                SignalResult::Deadline,
                Err(ControllerError::BudgetExceeded),
            ),
        ] {
            let evidence = SignalEvidence::rust_pidfd(target, result);
            assert_eq!(evidence.reduce_for_shutdown(&request, target), expected);
        }
        assert_eq!(
            SignalEvidence::rust_pidfd(SignalTarget::SessionRoot, SignalResult::Sent)
                .reduce_for_shutdown(&request, SignalTarget::SessionRoot),
            Ok(())
        );
    }

    #[test]
    fn red_acquisition_binds_launcher_digest_before_membership() {
        let anchor = AnchorCapabilities::test_owned(fd(), None);
        let mut request = request(anchor.anchor_fd.as_raw_fd());
        request.runtime_identity_digest = "22".repeat(32);
        let prepared = Controller::prepare(request).unwrap();
        let mut inspector = Inspector;
        assert!(matches!(
            prepared.acquire(anchor, &mut inspector),
            Err(ControllerError::IdentityMismatch(
                RecoveryObligation::LauncherIdentity
            ))
        ));
    }

    #[test]
    fn red_inherited_anchor_is_validated_as_directory() {
        assert!(matches!(
            AnchorCapabilities::from_inherited(fd(), None),
            Err(ControllerError::MalformedRequest)
        ));
        let directory: OwnedFd = File::open(".").unwrap().into();
        let evidence: OwnedFd = std::fs::OpenOptions::new()
            .read(true)
            .open("Cargo.toml")
            .unwrap()
            .into();
        assert!(AnchorCapabilities::from_inherited(directory, Some(evidence)).is_ok());
    }

    #[test]
    fn red_recovery_budget_failure_is_typed_fail_closed_without_fd_allocation() {
        let anchor = AnchorCapabilities::test_owned(fd(), None);
        let mut request = request(anchor.anchor_fd.as_raw_fd());
        request.budget.max_recovery_steps = 1;
        let prepared = Controller::prepare(request).unwrap();
        let mut inspector = Inspector;
        let acquired = prepared.acquire(anchor, &mut inspector).unwrap();
        let mut bound = acquired.bind_membership(initial_snapshot()).unwrap();
        bound.budget.recovery().unwrap();
        let mut signals = Signals { fail_root: true };
        let pending = bound
            .request_shutdown(initial_snapshot(), &mut signals)
            .unwrap_err();
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::BudgetExceeded));
        let response = pending.finalize_fail_closed().response;
        assert_eq!(response.verdict, ControllerVerdict::FailClosed);
    }

    #[test]
    fn red_recovery_pending_is_terminal_fail_closed() {
        let bound = bound();
        let mut signals = Signals { fail_root: true };
        let pending = bound
            .request_shutdown(initial_snapshot(), &mut signals)
            .unwrap_err();
        let finalized = pending.finalize_fail_closed().response;
        assert_eq!(finalized.verdict, ControllerVerdict::FailClosed);
        assert_eq!(
            finalized.journal.last(),
            Some(&JournalEvent::Finalized {
                verdict: ControllerVerdict::FailClosed
            })
        );
        assert!(!finalized.obligations.is_empty());

        let anchor = AnchorCapabilities::test_owned(fd(), None);
        let mut request = request(anchor.anchor_fd.as_raw_fd());
        request.budget.max_recovery_steps = 1;
        let prepared = Controller::prepare(request).unwrap();
        let mut inspector = Inspector;
        let acquired = prepared.acquire(anchor, &mut inspector).unwrap();
        let mut bound = acquired.bind_membership(initial_snapshot()).unwrap();
        bound.budget.recovery().unwrap();
        let pending = bound
            .request_shutdown(initial_snapshot(), &mut signals)
            .unwrap_err();
        let finalized = pending.finalize_fail_closed().response;
        assert_eq!(finalized.verdict, ControllerVerdict::FailClosed);
        assert!(finalized
            .obligations
            .contains(&RecoveryObligation::BudgetExceeded));
    }

    #[test]
    fn red_fixture_matrix_executes_all_13() {
        let files = [
            "ancestor-swap",
            "deadline",
            "deployed-launcher-replacement",
            "endpoint-replacement-after-check",
            "late-fork",
            "malformed-transport",
            "marker-in-place",
            "member-gone",
            "membership-drift-before-shutdown",
            "membership-drift-provisioning",
            "pid-reuse",
            "sigint",
            "stale-controller",
        ];
        for file in files {
            let path = format!(
                "{}/../../tests/fixtures/rootless-controller-v1/{file}.json",
                env!("CARGO_MANIFEST_DIR")
            );
            let fixture: FixtureRecord =
                serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
            match fixture {
                FixtureRecord {
                    id: FixtureId::MarkerInPlace,
                    class: FixtureClass::Marker,
                    mutation:
                        FixtureMutation::MarkerInPlace {
                            same_inode,
                            content_changed,
                            mode_changed,
                            owner_changed,
                        },
                    expected: FixtureExpected::MarkerIdentity,
                } => {
                    assert!(same_inode && content_changed && mode_changed && owner_changed);
                    let mut inspector = Inspector;
                    let bundle = inspector
                        .acquire_from_anchor(AnchorCapabilities::test_owned(fd(), None), 4)
                        .unwrap();
                    assert!(matches!(
                        bundle.marker.revalidate(MarkerObservation {
                            identity: identity(2),
                            content_digest: [8; 32],
                            mode: 0o100600,
                            uid: 1000,
                        }),
                        Err(ControllerError::IdentityMismatch(
                            RecoveryObligation::MarkerIdentity
                        ))
                    ));
                }
                FixtureRecord {
                    id: FixtureId::DeployedLauncherReplacement,
                    class: FixtureClass::Launcher,
                    mutation:
                        FixtureMutation::DeployedLauncherReplacement {
                            verifier_closure,
                            launcher,
                            content_digest,
                        },
                    expected: FixtureExpected::EndpointReplacement,
                } => {
                    assert_eq!(verifier_closure, "unchanged");
                    assert_eq!(launcher, "replacement");
                    assert_eq!(content_digest, "changed");
                    let mut inspector = Inspector;
                    let bundle = inspector
                        .acquire_from_anchor(AnchorCapabilities::test_owned(fd(), None), 4)
                        .unwrap();
                    assert!(matches!(
                        bundle.launcher.revalidate(LauncherObservation {
                            identity: identity(3),
                            content_digest: [8; 32],
                        }),
                        Err(ControllerError::IdentityMismatch(
                            RecoveryObligation::EndpointReplacement
                        ))
                    ));
                }
                FixtureRecord {
                    id: FixtureId::AncestorSwap,
                    class: FixtureClass::Identity,
                    mutation:
                        FixtureMutation::AncestorSwap {
                            component,
                            replacement,
                        },
                    expected: FixtureExpected::MembershipChanged,
                } => {
                    assert_eq!(component, "ancestor");
                    assert!(replacement);
                    let mut inspector = Inspector;
                    let bundle = inspector
                        .acquire_from_anchor(AnchorCapabilities::test_owned(fd(), None), 4)
                        .unwrap();
                    assert!(matches!(
                        bundle.prefix.revalidate(identity(99)),
                        Err(ControllerError::IdentityMismatch(
                            RecoveryObligation::MembershipChanged
                        ))
                    ));
                }
                FixtureRecord {
                    id: FixtureId::MembershipDriftProvisioning,
                    class: FixtureClass::Membership,
                    mutation:
                        FixtureMutation::MembershipDriftProvisioning {
                            phase,
                            child_set,
                            starttime,
                        },
                    expected: FixtureExpected::MembershipChanged,
                } => {
                    assert_eq!(phase, "PROVISIONING");
                    assert_eq!(child_set, "changed");
                    assert_eq!(starttime, "same");
                    let anchor = AnchorCapabilities::test_owned(fd(), None);
                    let p = Controller::prepare(request(anchor.anchor_fd.as_raw_fd())).unwrap();
                    let mut i = Inspector;
                    let acquired = p.acquire(anchor, &mut i).unwrap();
                    let mut changed = initial_snapshot();
                    changed.members[0].starttime = 99;
                    let pending = acquired.bind_membership(changed).unwrap_err();
                    assert!(matches!(
                        pending.error(),
                        ControllerError::MembershipChanged
                    ));
                }
                FixtureRecord {
                    id: FixtureId::MembershipDriftBeforeShutdown,
                    class: FixtureClass::Membership,
                    mutation:
                        FixtureMutation::MembershipDriftBeforeShutdown {
                            phase,
                            child_set,
                            pidfd_coverage,
                        },
                    expected: FixtureExpected::MembershipChanged,
                } => {
                    assert_eq!(phase, "PRE_SHUTDOWN");
                    assert_eq!(child_set, "changed");
                    assert_eq!(pidfd_coverage, "incomplete");
                    let bound = bound();
                    let mut changed = initial_snapshot();
                    changed.members.clear();
                    let mut signals = Signals { fail_root: false };
                    let pending = bound.request_shutdown(changed, &mut signals).unwrap_err();
                    assert_eq!(
                        pending.obligations(),
                        &[RecoveryObligation::MembershipChanged]
                    );
                }
                FixtureRecord {
                    id: FixtureId::MemberGone,
                    class: FixtureClass::Membership,
                    mutation:
                        FixtureMutation::MemberGone {
                            member,
                            pidfd,
                            signal,
                        },
                    expected: FixtureExpected::MembershipChanged,
                } => {
                    assert_eq!(member, "GONE");
                    assert_eq!(pidfd, "retained");
                    assert_eq!(signal, "not-sent");
                    let bound = bound();
                    let mut changed = initial_snapshot();
                    changed.members.clear();
                    let mut signals = Signals { fail_root: false };
                    let pending = bound.request_shutdown(changed, &mut signals).unwrap_err();
                    assert_eq!(
                        pending.obligations(),
                        &[RecoveryObligation::MembershipChanged]
                    );
                }
                FixtureRecord {
                    id: FixtureId::LateFork,
                    class: FixtureClass::Membership,
                    mutation:
                        FixtureMutation::LateFork {
                            phase,
                            new_child,
                            observed_by_controller,
                        },
                    expected: FixtureExpected::LateFork,
                } => {
                    assert_eq!(phase, "DRAIN");
                    assert!(new_child);
                    assert!(!observed_by_controller);
                    let bound = bound();
                    let mut changed = initial_snapshot();
                    changed.members.push(ProcessIdentity {
                        pid: 12,
                        starttime: 22,
                    });
                    let mut signals = Signals { fail_root: false };
                    let pending = bound.request_shutdown(changed, &mut signals).unwrap_err();
                    assert_eq!(pending.obligations(), &[RecoveryObligation::LateFork]);
                }
                FixtureRecord {
                    id: FixtureId::PidReuse,
                    class: FixtureClass::Membership,
                    mutation:
                        FixtureMutation::PidReuse {
                            pid,
                            starttime,
                            pidfd,
                        },
                    expected: FixtureExpected::PidReuse,
                } => {
                    assert_eq!(pid, "same");
                    assert_eq!(starttime, "different");
                    assert_eq!(pidfd, "retained");
                    let bound = bound();
                    let mut changed = initial_snapshot();
                    changed.members[0].starttime = 99;
                    let mut signals = Signals { fail_root: false };
                    let pending = bound.request_shutdown(changed, &mut signals).unwrap_err();
                    assert_eq!(pending.obligations(), &[RecoveryObligation::PidReuse]);
                }
                FixtureRecord {
                    id: FixtureId::EndpointReplacementAfterCheck,
                    class: FixtureClass::Cleanup,
                    mutation:
                        FixtureMutation::EndpointReplacementAfterCheck {
                            phase,
                            same_name,
                            identity: identity_name,
                            operation,
                        },
                    expected: FixtureExpected::EndpointReplacement,
                } => {
                    assert_eq!(phase, "AFTER_LAST_VERIFY");
                    assert!(same_name);
                    assert_eq!(identity_name, "replacement");
                    assert_eq!(operation, "FD_RELATIVE_UNLINK");
                    let quiescent = quiescent_for_test();
                    let mut cleanup = Cleanup {
                        replacement_after_verify: true,
                        verified: false,
                        replacement_preserved: false,
                        replacement_identity: None,
                        unlink_attempted: false,
                    };
                    let pending = quiescent.cleanup(&mut cleanup).unwrap_err();
                    assert_eq!(
                        pending.obligations(),
                        &[RecoveryObligation::EndpointReplacement]
                    );
                    assert!(cleanup.verified);
                    assert!(cleanup.replacement_preserved);
                    assert_eq!(cleanup.replacement_identity, Some(identity(999)));
                    assert!(!cleanup.unlink_attempted);
                }
                FixtureRecord {
                    id: FixtureId::Sigint,
                    class: FixtureClass::Interrupt,
                    mutation: FixtureMutation::Sigint { phase, rollback },
                    expected: FixtureExpected::ControllerInterrupted,
                } => {
                    assert_eq!(phase, "ANY_CHECKPOINT");
                    assert_eq!(rollback, "typed");
                    let bound = bound();
                    let mut signals = Signals { fail_root: true };
                    let pending = bound
                        .request_shutdown(initial_snapshot(), &mut signals)
                        .unwrap_err();
                    assert_eq!(pending.error(), &ControllerError::Interrupted);
                    assert_eq!(
                        pending.obligations(),
                        &[RecoveryObligation::ControllerInterrupted]
                    );
                }
                FixtureRecord {
                    id: FixtureId::MalformedTransport,
                    class: FixtureClass::Protocol,
                    mutation: FixtureMutation::MalformedTransport { input, size },
                    expected: FixtureExpected::MalformedRequest,
                } => {
                    assert_eq!(input, "unknown-field");
                    assert_eq!(size, "oversized");
                    let mut r = request(3);
                    r.anchor_fd = -1;
                    assert!(Controller::prepare(r).is_err());
                    let mut oversized = request(3);
                    oversized.transaction_id = "x".repeat(MAX_TRANSACTION_ID_BYTES + 1);
                    assert!(Controller::prepare(oversized).is_err());
                    let mut unknown = serde_json::to_value(request(3)).unwrap();
                    unknown["forged_kernel_fact"] = serde_json::Value::Bool(true);
                    assert!(serde_json::from_value::<ControllerRequest>(unknown).is_err());
                }
                FixtureRecord {
                    id: FixtureId::StaleController,
                    class: FixtureClass::Provenance,
                    mutation:
                        FixtureMutation::StaleController {
                            verifier_closure,
                            controller_binary,
                        },
                    expected: FixtureExpected::MalformedRequest,
                } => {
                    assert_eq!(verifier_closure, "same");
                    assert_eq!(controller_binary, "stale");
                    let mut r = request(3);
                    r.controller_closure_sha256 = "f".repeat(64);
                    assert!(matches!(
                        Controller::prepare(r),
                        Err(ControllerError::ClosureMismatch)
                    ));
                }
                FixtureRecord {
                    id: FixtureId::Deadline,
                    class: FixtureClass::Budget,
                    mutation: FixtureMutation::Deadline { phase, deadline },
                    expected: FixtureExpected::BudgetExceeded,
                } => {
                    assert_eq!(phase, "SHUTDOWN");
                    assert_eq!(deadline, "expired");
                    let anchor = AnchorCapabilities::test_owned(fd(), None);
                    let mut r = request(anchor.anchor_fd.as_raw_fd());
                    r.budget.max_virtual_time_ns = 1;
                    let p = Controller::prepare(r).unwrap();
                    let mut i = Inspector;
                    let acquired = p.acquire(anchor, &mut i).unwrap();
                    let pending = acquired
                        .bind_membership(MembershipSnapshot {
                            source: MembershipSource::RustProcTaskChildren,
                            complete: true,
                            root: Some(ProcessIdentity {
                                pid: 10,
                                starttime: 20,
                            }),
                            members: vec![ProcessIdentity {
                                pid: 11,
                                starttime: 21,
                            }],
                        })
                        .unwrap_err();
                    assert!(matches!(pending.error(), ControllerError::BudgetExceeded));
                }
                _ => panic!("fixture did not match its closed id/class/mutation/expected contract"),
            }
        }
    }
}
