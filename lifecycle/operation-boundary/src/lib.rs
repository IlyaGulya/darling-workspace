//! Authoritative lifecycle operation boundary for dar-4ush.2.
//!
//! The public API is intentionally capability-shaped.  Paths are accepted
//! only by [`Boundary::anchor_directory`].  All later operations use retained
//! directory/file/pidfd/lock capabilities, and the capability newtypes own
//! their `OwnedFd` with RAII.  No capability implements `Clone`.

use libc::{self, c_int, c_void, stat as libc_stat};
use serde::{Deserialize, Serialize};
use std::ffi::{CStr, CString, OsStr};
use std::fmt;
use std::io;
use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::ptr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

pub mod cohort_routing;
pub mod controller;
pub mod explorer;
pub mod fuzz;
pub mod guest_namespace_authority;
pub mod guest_namespace_transaction;
pub mod guest_ready;
pub mod linux_backend;
pub mod preinit_var_run;
pub mod quarantine_gc;
pub mod runtime_lower_binding;
pub mod state;

pub type Result<T> = std::result::Result<T, BoundaryError>;

#[derive(Debug)]
pub enum BoundaryError {
    Io {
        operation: &'static str,
        source: io::Error,
    },
    InvalidComponent(String),
    NotAbsolutePath,
    IdentityMismatch,
    WrongCapability(&'static str),
    CapabilityClosed,
    BudgetExceeded(&'static str),
    QuarantineRequired,
    Injected(Checkpoint),
    Unsupported(&'static str),
}

impl fmt::Display for BoundaryError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io { operation, source } => write!(formatter, "{operation}: {source}"),
            Self::InvalidComponent(component) => {
                write!(formatter, "invalid component: {component}")
            }
            Self::NotAbsolutePath => write!(formatter, "anchor path must be absolute"),
            Self::IdentityMismatch => write!(formatter, "retained inode identity mismatch"),
            Self::WrongCapability(operation) => {
                write!(formatter, "wrong capability for {operation}")
            }
            Self::CapabilityClosed => write!(formatter, "capability is closed or moved"),
            Self::BudgetExceeded(operation) => write!(formatter, "budget exceeded: {operation}"),
            Self::QuarantineRequired => {
                write!(
                    formatter,
                    "quarantine requires an externally proven quiescent scope"
                )
            }
            Self::Injected(checkpoint) => write!(formatter, "fault injected at {checkpoint:?}"),
            Self::Unsupported(operation) => write!(formatter, "unsupported: {operation}"),
        }
    }
}

impl std::error::Error for BoundaryError {}

fn io_error(operation: &'static str) -> BoundaryError {
    BoundaryError::Io {
        operation,
        source: io::Error::last_os_error(),
    }
}

fn c_name(name: &str) -> Result<CString> {
    c_name_bytes(name.as_bytes())
}

fn c_name_bytes(name: &[u8]) -> Result<CString> {
    if name.is_empty() || name == b"." || name == b".." || name.contains(&b'/') || name.contains(&0)
    {
        return Err(BoundaryError::InvalidComponent(
            String::from_utf8_lossy(name).into_owned(),
        ));
    }
    CString::new(name)
        .map_err(|_| BoundaryError::InvalidComponent(String::from_utf8_lossy(name).into_owned()))
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
    pub mode: u32,
    pub nlink: u64,
    pub uid: u32,
    pub gid: u32,
}

/// Runtime brand carried by every capability rooted at one anchored
/// transaction scope.  The value is intentionally private and capabilities
/// are not constructible outside this module, so a lease from another root
/// cannot be supplied to a mutation API by accident or by forging an id.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ScopeId(u64);

static NEXT_SCOPE_ID: AtomicU64 = AtomicU64::new(1);

fn fresh_scope() -> ScopeId {
    ScopeId(NEXT_SCOPE_ID.fetch_add(1, Ordering::Relaxed))
}

impl FileIdentity {
    fn from_stat(value: &libc_stat) -> Self {
        Self {
            device: value.st_dev,
            inode: value.st_ino,
            mode: value.st_mode,
            nlink: value.st_nlink,
            uid: value.st_uid,
            gid: value.st_gid,
        }
    }

    pub(crate) fn from_fd(fd: RawFd) -> Result<Self> {
        let mut value = MaybeUninit::<libc_stat>::zeroed();
        // SAFETY: fstat initializes the supplied stat buffer on success.
        let result = unsafe { libc::fstat(fd, value.as_mut_ptr()) };
        if result < 0 {
            return Err(io_error("fstat"));
        }
        // SAFETY: fstat returned success, so the buffer is initialized.
        Ok(Self::from_stat(unsafe { &value.assume_init() }))
    }

    pub(crate) fn from_at(parent: RawFd, name: &CString) -> Result<Self> {
        let mut value = MaybeUninit::<libc_stat>::zeroed();
        // SAFETY: fstatat initializes the supplied stat buffer on success;
        // AT_SYMLINK_NOFOLLOW keeps the name itself from becoming authority.
        let result = unsafe {
            libc::fstatat(
                parent,
                name.as_ptr(),
                value.as_mut_ptr(),
                libc::AT_SYMLINK_NOFOLLOW,
            )
        };
        if result < 0 {
            return Err(io_error("fstatat"));
        }
        // SAFETY: fstatat returned success, so the buffer is initialized.
        Ok(Self::from_stat(unsafe { &value.assume_init() }))
    }

    pub fn inode_key(self) -> (u64, u64) {
        (self.device, self.inode)
    }
}

macro_rules! capability_type {
    ($name:ident, $label:literal) => {
        pub struct $name {
            fd: OwnedFd,
            identity: FileIdentity,
            id: u64,
            #[allow(dead_code)]
            scope: ScopeId,
        }

        impl $name {
            fn new(fd: OwnedFd, identity: FileIdentity, id: u64, scope: ScopeId) -> Self {
                Self {
                    fd,
                    identity,
                    id,
                    scope,
                }
            }

            pub fn identity(&self) -> FileIdentity {
                self.identity
            }

            pub fn id(&self) -> u64 {
                self.id
            }

            #[allow(dead_code)]
            fn scope(&self) -> ScopeId {
                self.scope
            }

            fn raw_fd(&self) -> RawFd {
                self.fd.as_raw_fd()
            }
        }

        impl fmt::Debug for $name {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                formatter
                    .debug_struct($label)
                    .field("id", &self.id)
                    .field("identity", &self.identity)
                    .finish()
            }
        }
    };
}

capability_type!(DirCap, "DirCap");
capability_type!(FileCap, "FileCap");
capability_type!(PidFdCap, "PidFdCap");
capability_type!(LockCap, "LockCap");
capability_type!(ExclusiveLease, "ExclusiveLease");

/// An externally established proof that no namespace writer can mutate the
/// anchored parent for the duration of one final exact mutation or quarantine
/// GC transaction.
///
/// The operation boundary deliberately has no constructor that discovers or
/// asserts quiescence.  A lifecycle controller/explorer must establish the
/// writer-stop barrier and then create this value through the explicit unsafe
/// handoff.  Without this scope, [`QuarantinedObject`] remains a recovery
/// obligation and cannot be unlinked by the boundary.
pub struct QuiescentScope {
    parent_identity: FileIdentity,
    secondary_parent_identity: Option<FileIdentity>,
    secondary_parent_scope: Option<ScopeId>,
    lease_identity: FileIdentity,
    scope: ScopeId,
    token: u64,
}

impl QuiescentScope {
    /// Construct a scope only after an external controller has proved that
    /// all namespace writers for this parent have stopped.  The boundary
    /// cannot establish that fact itself; callers must uphold the safety
    /// contract for the lifetime of the returned scope.
    ///
    /// # Safety
    /// The caller must have a real writer-stop/quiescence barrier for the
    /// parent and lease.  Violating that promise reintroduces a namespace
    /// race around the final unlink.
    pub unsafe fn from_external(parent: &DirCap, lease: &ExclusiveLease) -> Result<Self> {
        if parent.scope() != lease.scope() {
            return Err(BoundaryError::WrongCapability("quiescent scope"));
        }
        let parent_identity = FileIdentity::from_fd(parent.raw_fd())?;
        let lease_identity = FileIdentity::from_fd(lease.raw_fd())?;
        Ok(Self {
            parent_identity,
            secondary_parent_identity: None,
            secondary_parent_scope: None,
            lease_identity,
            scope: parent.scope(),
            token: NEXT_SCOPE_ID.fetch_add(1, Ordering::Relaxed),
        })
    }

    /// Construct a scope for one cross-directory mutation.  Both parent
    /// identities are captured under the same externally established writer
    /// barrier; neither directory may be rediscovered by pathname later.
    ///
    /// # Safety
    /// The caller must have stopped writers for both parents for the lifetime
    /// of the returned scope.
    pub unsafe fn from_external_pair(
        source: &DirCap,
        destination: &DirCap,
        lease: &ExclusiveLease,
    ) -> Result<Self> {
        if source.scope() != lease.scope() {
            return Err(BoundaryError::WrongCapability("quiescent scope"));
        }
        let parent_identity = FileIdentity::from_fd(source.raw_fd())?;
        let secondary_parent_identity = FileIdentity::from_fd(destination.raw_fd())?;
        let lease_identity = FileIdentity::from_fd(lease.raw_fd())?;
        Ok(Self {
            parent_identity,
            secondary_parent_identity: Some(secondary_parent_identity),
            secondary_parent_scope: Some(destination.scope()),
            lease_identity,
            scope: source.scope(),
            token: NEXT_SCOPE_ID.fetch_add(1, Ordering::Relaxed),
        })
    }

    fn allows_parent(&self, identity: FileIdentity) -> bool {
        let inode = identity.inode_key();
        self.parent_identity.inode_key() == inode
            || self
                .secondary_parent_identity
                .is_some_and(|secondary| secondary.inode_key() == inode)
    }

    pub fn token(&self) -> u64 {
        self.token
    }
}

/// A verified object moved out of its public name and retained as an explicit
/// recovery obligation.  It is intentionally non-Clone and can only be
/// collected by [`Boundary::gc_quarantine`] with an externally proven
/// [`QuiescentScope`].
#[must_use = "quarantine obligations must be handled"]
pub struct QuarantinedObject {
    fd: OwnedFd,
    // Retain the exact parent authority with the quarantined inode.  A
    // recovery controller must not rediscover the parent by pathname after
    // the move; this capability also lets it build a matching quiescence
    // scope for nested staging directories.
    parent: DirCap,
    parent_identity: FileIdentity,
    lease_identity: FileIdentity,
    scope: ScopeId,
    quarantine_name: CString,
    expected: FileIdentity,
    directory: bool,
    id: u64,
}

impl QuarantinedObject {
    pub fn id(&self) -> u64 {
        self.id
    }

    pub fn identity(&self) -> FileIdentity {
        self.expected
    }

    pub fn parent_identity(&self) -> FileIdentity {
        self.parent_identity
    }

    pub fn lease_identity(&self) -> FileIdentity {
        self.lease_identity
    }

    pub fn quarantine_name(&self) -> &CStr {
        &self.quarantine_name
    }

    pub fn is_directory(&self) -> bool {
        self.directory
    }

    pub(crate) fn parent_cap(&self) -> &DirCap {
        &self.parent
    }
}

impl fmt::Debug for QuarantinedObject {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("QuarantinedObject")
            .field("id", &self.id)
            .field("expected", &self.expected)
            .field("directory", &self.directory)
            .finish()
    }
}

#[must_use = "quarantine obligations must be handled"]
pub struct UnboundQuarantine {
    parent_fd: Option<OwnedFd>,
    parent_identity: FileIdentity,
    lease_identity: FileIdentity,
    scope: ScopeId,
    quarantine_name: CString,
    expected: FileIdentity,
    directory: bool,
    id: u64,
}

impl UnboundQuarantine {
    pub fn id(&self) -> u64 {
        self.id
    }

    pub fn identity(&self) -> FileIdentity {
        self.expected
    }

    pub fn parent_identity(&self) -> FileIdentity {
        self.parent_identity
    }

    pub fn lease_identity(&self) -> FileIdentity {
        self.lease_identity
    }

    pub fn quarantine_name(&self) -> &CStr {
        &self.quarantine_name
    }

    pub fn is_directory(&self) -> bool {
        self.directory
    }

    pub(crate) fn parent_fd(&self) -> Option<RawFd> {
        self.parent_fd.as_ref().map(AsRawFd::as_raw_fd)
    }
}

impl fmt::Debug for UnboundQuarantine {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("UnboundQuarantine")
            .field("id", &self.id)
            .field("parent_identity", &self.parent_identity)
            .field("lease_identity", &self.lease_identity)
            .field("scope", &self.scope)
            .field("quarantine_name", &self.quarantine_name)
            .field("expected", &self.expected)
            .field("directory", &self.directory)
            .finish()
    }
}

#[must_use = "quarantine obligations must be handled"]
#[derive(Debug)]
pub enum QuarantineObligation {
    Bound(QuarantinedObject),
    Unbound(UnboundQuarantine),
}

impl QuarantineObligation {
    pub fn id(&self) -> u64 {
        match self {
            Self::Bound(object) => object.id(),
            Self::Unbound(object) => object.id(),
        }
    }

    pub fn is_bound(&self) -> bool {
        matches!(self, Self::Bound(_))
    }
}

#[must_use = "quarantine obligations must be handled"]
#[derive(Debug)]
pub struct QuarantineObligations {
    obligations: Vec<QuarantineObligation>,
}

impl QuarantineObligations {
    pub fn len(&self) -> usize {
        self.obligations.len()
    }

    pub fn is_empty(&self) -> bool {
        self.obligations.is_empty()
    }

    pub fn ids(&self) -> Vec<u64> {
        self.obligations
            .iter()
            .map(QuarantineObligation::id)
            .collect()
    }

    pub fn into_inner(self) -> Vec<QuarantineObligation> {
        self.obligations
    }
}

/// A staging resource that could not reach publication or safe cleanup.
/// Stage obligations are separate from quarantine obligations because the
/// resource may still be reachable through its retained staging parent.
#[must_use = "stage obligations must be handled"]
#[derive(Debug)]
pub enum StageObligation {
    Bound(BoundStageObligation),
    Unbound(UnboundStageObligation),
}

#[must_use = "bound stage obligations must be handled"]
#[derive(Debug)]
pub struct BoundStageObligation {
    parent: DirCap,
    stage: DirCap,
    stage_ref: StageRef,
    name: CString,
    child: Option<ChildRef>,
    child_identity: Option<FileIdentity>,
}

impl BoundStageObligation {
    pub fn id(&self) -> u64 {
        self.stage_ref.id()
    }

    pub fn child_id(&self) -> Option<u64> {
        self.child.map(ChildRef::id)
    }

    pub fn stage_identity(&self) -> FileIdentity {
        self.stage.identity()
    }

    pub fn child_identity(&self) -> Option<FileIdentity> {
        self.child_identity
    }

    pub fn name(&self) -> &CStr {
        &self.name
    }
}

#[must_use = "unbound stage obligations must be handled"]
#[derive(Debug)]
pub struct UnboundStageObligation {
    parent_fd: Option<OwnedFd>,
    parent_identity: FileIdentity,
    stage_ref: StageRef,
    child: Option<ChildRef>,
    name: CString,
    expected: Option<FileIdentity>,
    child_identity: Option<FileIdentity>,
    scope: ScopeId,
}

impl UnboundStageObligation {
    pub fn id(&self) -> u64 {
        self.stage_ref.id()
    }

    pub fn child_id(&self) -> Option<u64> {
        self.child.map(ChildRef::id)
    }

    pub fn parent_identity(&self) -> FileIdentity {
        self.parent_identity
    }

    pub fn expected_identity(&self) -> Option<FileIdentity> {
        self.expected
    }

    pub fn child_identity(&self) -> Option<FileIdentity> {
        self.child_identity
    }

    pub fn name(&self) -> &CStr {
        &self.name
    }

    pub fn has_retained_parent(&self) -> bool {
        self.parent_fd.is_some()
    }

    #[allow(dead_code)]
    fn scope(&self) -> ScopeId {
        self.scope
    }
}

impl StageObligation {
    pub fn id(&self) -> u64 {
        match self {
            Self::Bound(obligation) => obligation.id(),
            Self::Unbound(obligation) => obligation.id(),
        }
    }

    pub fn is_bound(&self) -> bool {
        matches!(self, Self::Bound(_))
    }
}

#[must_use = "stage obligations must be handled"]
#[derive(Debug)]
pub struct StageObligations {
    obligations: Vec<StageObligation>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum RollbackDisposition {
    ReadyForQuarantine,
    PreQuarantineFailure,
    RestoredAfterMove,
    QuarantinedForRecovery,
}

impl RollbackDisposition {
    fn retains_stage_authority(self) -> bool {
        matches!(self, Self::PreQuarantineFailure | Self::RestoredAfterMove)
    }
}

impl StageObligations {
    pub fn len(&self) -> usize {
        self.obligations.len()
    }

    pub fn is_empty(&self) -> bool {
        self.obligations.is_empty()
    }

    pub fn ids(&self) -> Vec<u64> {
        self.obligations.iter().map(StageObligation::id).collect()
    }

    /// Return the retained stage identity carried by each obligation.  Bound
    /// records read from their retained directory FD; unbound records expose
    /// the identity captured before the fallible bind step.
    pub fn identities(&self) -> Vec<Option<FileIdentity>> {
        self.obligations
            .iter()
            .map(|obligation| match obligation {
                StageObligation::Bound(stage) => Some(stage.stage_identity()),
                StageObligation::Unbound(stage) => stage.expected_identity(),
            })
            .collect()
    }

    pub fn child_identities(&self) -> Vec<Option<FileIdentity>> {
        self.obligations
            .iter()
            .map(|obligation| match obligation {
                StageObligation::Bound(stage) => stage.child_identity(),
                StageObligation::Unbound(stage) => stage.child_identity(),
            })
            .collect()
    }

    pub fn into_inner(self) -> Vec<StageObligation> {
        self.obligations
    }
}

#[must_use = "boundary parts contain recovery obligations"]
pub struct BoundaryParts<O> {
    pub observer: O,
    pub quarantine_obligations: QuarantineObligations,
    pub stage_obligations: StageObligations,
}

#[must_use = "boundary finish failed with recovery obligations"]
pub struct BoundaryFinishError<O> {
    pub observer: O,
    pub quarantine_obligations: QuarantineObligations,
    pub stage_obligations: StageObligations,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationName {
    ClockNow,
    AnchorDirectory,
    OpenChild,
    OpenLock,
    MkdirChild,
    Revalidate,
    RevalidateMetadata,
    ListNames,
    Read,
    Write,
    Fsync,
    UnlinkExact,
    RenameExact,
    PidfdOpen,
    PidfdSignal,
    Flock,
    Close,
    Checkpoint,
    StageRegistered,
    StagePublished,
    StageCleanup,
    Quarantine,
}

/// Closed set of outcomes emitted by the operation journal.
///
/// This is deliberately not constructible from a string.  Producers must
/// select an outcome at compile time, so a typo cannot become a runtime
/// journal event (or a panic in the boundary).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum OperationOutcome {
    Observed,
    Applied,
    Error,
    Injected,
    Allocated,
    Registered,
    Published,
    Deferred,
    StagedError,
    RolledBackError,
    RollbackIncomplete,
    IdentityMismatch,
    MutatedError,
}

impl OperationOutcome {
    fn mutation(self) -> MutationState {
        match self {
            Self::Applied | Self::Allocated | Self::Registered | Self::Published => {
                MutationState::Applied
            }
            Self::Observed | Self::Error | Self::Injected | Self::Deferred => {
                MutationState::NotAttempted
            }
            Self::RolledBackError => MutationState::RolledBack,
            Self::RollbackIncomplete => MutationState::RollbackIncomplete,
            Self::StagedError | Self::IdentityMismatch | Self::MutatedError => {
                MutationState::Partial
            }
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum MutationState {
    NotAttempted,
    Applied,
    Partial,
    RolledBack,
    RollbackIncomplete,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum IdentityObservation {
    NotApplicable,
    Observed(FileIdentity),
}

impl IdentityObservation {
    pub fn is_observed(self) -> bool {
        matches!(self, Self::Observed(_))
    }
}

impl MutationState {
    pub fn changed(self) -> bool {
        !matches!(self, Self::NotAttempted)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CapabilityRole {
    Directory,
    File,
    Lease,
    Lock,
    Pidfd,
    Parent,
    SourceParent,
    DestinationParent,
    Stage,
    Child,
    Quarantine,
    Primary,
    Operand,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct CapabilityRef {
    id: u64,
    role: CapabilityRole,
}

impl CapabilityRef {
    pub fn id(self) -> u64 {
        self.id
    }

    pub fn role(self) -> CapabilityRole {
        self.role
    }
}

/// Typed journal references for resources which are staged before they become
/// a public capability.  Keeping these distinct from raw capability IDs
/// prevents role inference from a positional integer vector.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct StageRef(u64);

impl StageRef {
    fn new(id: u64) -> Self {
        Self(id)
    }

    pub fn id(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ChildRef(u64);

impl ChildRef {
    fn new(id: u64) -> Self {
        Self(id)
    }

    pub fn id(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct QuarantineRef(u64);

impl QuarantineRef {
    fn new(id: u64) -> Self {
        Self(id)
    }

    pub fn id(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct CapabilitySet {
    entries: Vec<CapabilityRef>,
}

impl CapabilitySet {
    fn from_refs(entries: impl IntoIterator<Item = CapabilityRef>) -> Self {
        Self {
            entries: entries.into_iter().collect(),
        }
    }

    pub fn empty() -> Self {
        Self::from_refs([])
    }

    pub fn anchor(directory: &DirCap) -> Self {
        Self::from_refs([CapabilityRef {
            id: directory.id(),
            role: CapabilityRole::Primary,
        }])
    }

    pub fn directory(directory: &DirCap) -> Self {
        Self::from_refs([CapabilityRef {
            id: directory.id(),
            role: CapabilityRole::Directory,
        }])
    }

    pub fn open_file(parent: &DirCap, child: &FileCap) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: parent.id(),
                role: CapabilityRole::Parent,
            },
            CapabilityRef {
                id: child.id(),
                role: CapabilityRole::File,
            },
        ])
    }

    pub fn open_directory(parent: &DirCap, child: &DirCap) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: parent.id(),
                role: CapabilityRole::Parent,
            },
            CapabilityRef {
                id: child.id(),
                role: CapabilityRole::Child,
            },
        ])
    }

    pub fn open_lock(parent: &DirCap, lock: &LockCap) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: parent.id(),
                role: CapabilityRole::Parent,
            },
            CapabilityRef {
                id: lock.id(),
                role: CapabilityRole::Lock,
            },
        ])
    }

    pub fn mkdir_start(parent: &DirCap, lease: &ExclusiveLease) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: parent.id(),
                role: CapabilityRole::Parent,
            },
            CapabilityRef {
                id: lease.id(),
                role: CapabilityRole::Lease,
            },
        ])
    }

    pub fn mkdir_child(
        parent: &DirCap,
        lease: &ExclusiveLease,
        stage: Option<StageRef>,
        child: Option<ChildRef>,
    ) -> Self {
        let mut entries = vec![
            CapabilityRef {
                id: parent.id(),
                role: CapabilityRole::Parent,
            },
            CapabilityRef {
                id: lease.id(),
                role: CapabilityRole::Lease,
            },
        ];
        if let Some(stage) = stage {
            entries.push(CapabilityRef {
                id: stage.id(),
                role: CapabilityRole::Stage,
            });
        }
        if let Some(child) = child {
            entries.push(CapabilityRef {
                id: child.id(),
                role: CapabilityRole::Child,
            });
        }
        Self::from_refs(entries)
    }

    pub fn stage(
        parent: &DirCap,
        lease: &ExclusiveLease,
        stage: StageRef,
        child: Option<ChildRef>,
    ) -> Self {
        Self::mkdir_child(parent, lease, Some(stage), child)
    }

    pub fn publish(
        source_parent: &DirCap,
        destination_parent: &DirCap,
        lease: &ExclusiveLease,
        stage: StageRef,
        child: ChildRef,
    ) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: source_parent.id(),
                role: CapabilityRole::SourceParent,
            },
            CapabilityRef {
                id: destination_parent.id(),
                role: CapabilityRole::DestinationParent,
            },
            CapabilityRef {
                id: lease.id(),
                role: CapabilityRole::Lease,
            },
            CapabilityRef {
                id: stage.id(),
                role: CapabilityRole::Stage,
            },
            CapabilityRef {
                id: child.id(),
                role: CapabilityRole::Child,
            },
        ])
    }

    pub fn unlink(parent: &DirCap, lease: &ExclusiveLease) -> Self {
        Self::mkdir_start(parent, lease)
    }

    pub fn rename(
        source_parent: &DirCap,
        destination_parent: &DirCap,
        lease: &ExclusiveLease,
    ) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: source_parent.id(),
                role: CapabilityRole::SourceParent,
            },
            CapabilityRef {
                id: destination_parent.id(),
                role: CapabilityRole::DestinationParent,
            },
            CapabilityRef {
                id: lease.id(),
                role: CapabilityRole::Lease,
            },
        ])
    }

    pub fn file(file: &FileCap) -> Self {
        Self::from_refs([CapabilityRef {
            id: file.id(),
            role: CapabilityRole::File,
        }])
    }

    pub fn write(file: &FileCap, lease: &ExclusiveLease) -> Self {
        Self::from_refs([
            CapabilityRef {
                id: file.id(),
                role: CapabilityRole::File,
            },
            CapabilityRef {
                id: lease.id(),
                role: CapabilityRole::Lease,
            },
        ])
    }

    pub fn pidfd(pidfd: &PidFdCap) -> Self {
        Self::from_refs([CapabilityRef {
            id: pidfd.id(),
            role: CapabilityRole::Pidfd,
        }])
    }

    pub fn lock(lock: &LockCap) -> Self {
        Self::from_refs([CapabilityRef {
            id: lock.id(),
            role: CapabilityRole::Lock,
        }])
    }

    pub fn lease(lease: &ExclusiveLease) -> Self {
        Self::from_refs([CapabilityRef {
            id: lease.id(),
            role: CapabilityRole::Lease,
        }])
    }

    pub fn quarantine(object: QuarantineRef) -> Self {
        Self::from_refs([CapabilityRef {
            id: object.id(),
            role: CapabilityRole::Quarantine,
        }])
    }

    pub fn close_directory(capability: &DirCap) -> Self {
        Self::from_refs([CapabilityRef {
            id: capability.id(),
            role: CapabilityRole::Directory,
        }])
    }

    pub fn close_file(capability: &FileCap) -> Self {
        Self::file(capability)
    }

    pub fn close_pidfd(capability: &PidFdCap) -> Self {
        Self::pidfd(capability)
    }

    pub fn close_lock(capability: &LockCap) -> Self {
        Self::lock(capability)
    }

    pub fn close_lease(capability: &ExclusiveLease) -> Self {
        Self::lease(capability)
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn as_slice(&self) -> &[CapabilityRef] {
        &self.entries
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum StageCleanupTerminal {
    Cleaned,
    Deferred,
    Failed,
    RollbackIncomplete,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum QuarantineTerminal {
    Restored,
    BoundRecovery,
    UnboundRecovery,
    Collected,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StageJournal {
    Allocated {
        capabilities: CapabilitySet,
        stage: StageRef,
        identity: IdentityObservation,
    },
    Registered {
        capabilities: CapabilitySet,
        stage: StageRef,
        child: ChildRef,
        identity: IdentityObservation,
    },
    Published {
        capabilities: CapabilitySet,
        stage: StageRef,
        child: ChildRef,
        identity: IdentityObservation,
    },
    Cleanup {
        capabilities: CapabilitySet,
        stage: StageRef,
        terminal: StageCleanupTerminal,
        identity: IdentityObservation,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum QuarantineEvent {
    Restored {
        capabilities: CapabilitySet,
        object: QuarantineRef,
        identity: FileIdentity,
    },
    BoundRecovery {
        capabilities: CapabilitySet,
        object: QuarantineRef,
        identity: FileIdentity,
    },
    UnboundRecovery {
        capabilities: CapabilitySet,
        object: QuarantineRef,
        identity: FileIdentity,
    },
    Collected {
        capabilities: CapabilitySet,
        object: QuarantineRef,
        identity: FileIdentity,
    },
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationEvent {
    pub operation: OperationName,
    pub capabilities: CapabilitySet,
    pub result: OperationOutcome,
    pub checkpoint: Option<Checkpoint>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum JournalRecord {
    Operation(OperationEvent),
    Stage(StageJournal),
    Quarantine(QuarantineEvent),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationRecord {
    pub sequence: u64,
    pub record: JournalRecord,
}

impl OperationRecord {
    /// Compatibility views for focused tests and explorers.  The stored
    /// representation remains the exhaustive `JournalRecord`; these helpers
    /// never reconstruct producer payloads or infer capability roles.
    pub fn operation(&self) -> OperationName {
        match &self.record {
            JournalRecord::Operation(event) => event.operation,
            JournalRecord::Stage(StageJournal::Allocated { .. })
            | JournalRecord::Stage(StageJournal::Registered { .. }) => {
                OperationName::StageRegistered
            }
            JournalRecord::Stage(StageJournal::Published { .. }) => OperationName::StagePublished,
            JournalRecord::Stage(StageJournal::Cleanup { .. }) => OperationName::StageCleanup,
            JournalRecord::Quarantine(_) => OperationName::Quarantine,
        }
    }

    pub fn outcome(&self) -> OperationOutcome {
        match &self.record {
            JournalRecord::Operation(event) => event.result,
            JournalRecord::Stage(StageJournal::Allocated { .. }) => OperationOutcome::Allocated,
            JournalRecord::Stage(StageJournal::Registered { .. }) => OperationOutcome::Registered,
            JournalRecord::Stage(StageJournal::Published { .. }) => OperationOutcome::Published,
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::Cleaned,
                ..
            }) => OperationOutcome::Applied,
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::Deferred,
                ..
            }) => OperationOutcome::Deferred,
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::RollbackIncomplete,
                ..
            }) => OperationOutcome::RollbackIncomplete,
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::Failed,
                ..
            }) => OperationOutcome::Error,
            JournalRecord::Quarantine(_) => OperationOutcome::Applied,
        }
    }

    pub fn mutation(&self) -> MutationState {
        match &self.record {
            JournalRecord::Operation(event) => event.result.mutation(),
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::Cleaned,
                ..
            }) => MutationState::Applied,
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::Deferred | StageCleanupTerminal::Failed,
                ..
            }) => MutationState::Partial,
            JournalRecord::Stage(StageJournal::Cleanup {
                terminal: StageCleanupTerminal::RollbackIncomplete,
                ..
            }) => MutationState::RollbackIncomplete,
            JournalRecord::Stage(StageJournal::Allocated { .. })
            | JournalRecord::Stage(StageJournal::Registered { .. })
            | JournalRecord::Stage(StageJournal::Published { .. }) => MutationState::Applied,
            JournalRecord::Quarantine(_) => MutationState::Applied,
        }
    }

    pub fn checkpoint(&self) -> Option<Checkpoint> {
        match &self.record {
            JournalRecord::Operation(event) => event.checkpoint,
            JournalRecord::Stage(_) | JournalRecord::Quarantine(_) => None,
        }
    }

    pub fn identity(&self) -> IdentityObservation {
        match &self.record {
            JournalRecord::Operation(_) => IdentityObservation::NotApplicable,
            JournalRecord::Stage(StageJournal::Allocated { identity, .. })
            | JournalRecord::Stage(StageJournal::Registered { identity, .. })
            | JournalRecord::Stage(StageJournal::Published { identity, .. })
            | JournalRecord::Stage(StageJournal::Cleanup { identity, .. }) => *identity,
            JournalRecord::Quarantine(QuarantineEvent::Restored { identity, .. })
            | JournalRecord::Quarantine(QuarantineEvent::BoundRecovery { identity, .. })
            | JournalRecord::Quarantine(QuarantineEvent::UnboundRecovery { identity, .. })
            | JournalRecord::Quarantine(QuarantineEvent::Collected { identity, .. }) => {
                IdentityObservation::Observed(*identity)
            }
        }
    }

    pub fn journal(&self) -> &JournalRecord {
        &self.record
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Checkpoint {
    AfterMkdirBeforeBind,
    AfterBindBeforePublish,
    AfterStageMkdirBeforeBind,
    AfterStageBindBeforeMove,
    AfterMoveBeforeVerify,
    AfterQuarantineMoveBeforeVerify,
    AfterQuarantineVerifyBeforeGc,
    BeforeExactMutation,
    AfterExactMutation,
}

pub trait Observer {
    fn record(&mut self, record: OperationRecord);
}

#[derive(Default)]
pub struct VecObserver {
    pub records: Vec<OperationRecord>,
}

impl Observer for VecObserver {
    fn record(&mut self, record: OperationRecord) {
        self.records.push(record);
    }
}

/// Production does not retain an unbounded forensic journal in memory.  The
/// detailed `VecObserver` is reserved for focused tests and explorer runs;
/// deployed callers can provide a bounded sink of their own.
#[derive(Default)]
pub struct NoopObserver;

impl Observer for NoopObserver {
    fn record(&mut self, _record: OperationRecord) {}
}

pub trait Clock {
    fn now_ns(&mut self) -> u64;
    fn sleep_ns(&mut self, duration_ns: u64);
}

pub struct RealClock {
    start: Instant,
}

impl Default for RealClock {
    fn default() -> Self {
        Self {
            start: Instant::now(),
        }
    }
}

impl Clock for RealClock {
    fn now_ns(&mut self) -> u64 {
        self.start.elapsed().as_nanos() as u64
    }

    fn sleep_ns(&mut self, duration_ns: u64) {
        std::thread::sleep(std::time::Duration::from_nanos(duration_ns));
    }
}

#[derive(Clone, Debug)]
pub struct ScriptedClock {
    now: u64,
}

impl ScriptedClock {
    pub fn new(now: u64) -> Self {
        Self { now }
    }

    pub fn advance(&mut self, duration_ns: u64) {
        self.now = self.now.saturating_add(duration_ns);
    }
}

impl Clock for ScriptedClock {
    fn now_ns(&mut self) -> u64 {
        self.now
    }

    fn sleep_ns(&mut self, duration_ns: u64) {
        self.advance(duration_ns);
    }
}

pub trait FaultInjector {
    fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()>;

    /// Whether the most recent hook performed (or attempted) a namespace
    /// write.  A true result irreversibly invalidates any parked external
    /// quiescence proof; a controller must establish a fresh barrier before
    /// another exact mutation or recovery operation.
    fn invalidates_quiescence(&self) -> bool {
        false
    }

    /// Consume the invalidation for the hook just executed.  Keeping this a
    /// one-shot observation lets a caller establish a fresh barrier after a
    /// writer hook; a historical invalidation must not poison later scopes.
    fn take_quiescence_invalidation(&mut self) -> bool {
        self.invalidates_quiescence()
    }

    /// Run after a checkpoint has been observed successfully.  This second
    /// phase lets a deterministic test seam model an AFTER failure at the
    /// real operation boundary; production injectors keep the default no-op.
    fn after_checkpoint(&mut self, _checkpoint: Checkpoint) -> Result<()> {
        Ok(())
    }
}

#[derive(Default)]
pub struct NoFault;

impl FaultInjector for NoFault {
    fn checkpoint(&mut self, _checkpoint: Checkpoint) -> Result<()> {
        Ok(())
    }
}

pub struct FailAt {
    checkpoint: Checkpoint,
    fired: bool,
}

impl FailAt {
    pub fn new(checkpoint: Checkpoint) -> Self {
        Self {
            checkpoint,
            fired: false,
        }
    }
}

impl FaultInjector for FailAt {
    fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
        if checkpoint == self.checkpoint && !self.fired {
            self.fired = true;
            return Err(BoundaryError::Injected(checkpoint));
        }
        Ok(())
    }
}

fn open_dir_at(parent: RawFd, name: &CString) -> Result<OwnedFd> {
    let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW;
    // SAFETY: name is a NUL-terminated component and flags contain no
    // borrowed pointers. The returned fd is transferred to OwnedFd.
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
    if fd < 0 {
        return Err(io_error("openat directory"));
    }
    // SAFETY: fd is freshly returned by openat and uniquely owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn open_file_at(parent: RawFd, name: &CString, writable: bool) -> Result<OwnedFd> {
    let flags = (if writable {
        libc::O_RDWR
    } else {
        libc::O_RDONLY
    }) | libc::O_CLOEXEC
        | libc::O_NOFOLLOW;
    // SAFETY: name is a NUL-terminated component and flags contain no
    // borrowed pointers. The returned fd is transferred to OwnedFd.
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
    if fd < 0 {
        return Err(io_error("openat file"));
    }
    // SAFETY: fd is freshly returned by openat and uniquely owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn open_object_path_at(parent: RawFd, name: &CString) -> Result<OwnedFd> {
    let flags = libc::O_PATH | libc::O_CLOEXEC | libc::O_NOFOLLOW;
    // SAFETY: name is NUL-terminated and the returned descriptor is owned by
    // the caller. O_PATH permits identity binding without read permission.
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags) };
    if fd < 0 {
        return Err(io_error("openat object capability"));
    }
    // SAFETY: fd is freshly returned by openat and uniquely owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn open_root() -> Result<OwnedFd> {
    let path = CString::new("/").expect("literal has no NUL");
    let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW;
    // SAFETY: literal path and valid flags.
    let fd = unsafe { libc::open(path.as_ptr(), flags) };
    if fd < 0 {
        return Err(io_error("open root"));
    }
    // SAFETY: fd is freshly returned by open and uniquely owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn duplicate_fd(fd: RawFd) -> Result<OwnedFd> {
    // SAFETY: fcntl duplicates the retained descriptor and returns a new
    // descriptor owned by this function; CLOEXEC prevents capability leakage.
    let duplicated = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if duplicated < 0 {
        return Err(io_error("fcntl duplicate"));
    }
    // SAFETY: duplicated is a fresh descriptor returned by fcntl.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicated) })
}

fn mkdir_at(parent: RawFd, name: &CString, mode: u32) -> Result<()> {
    // SAFETY: name is NUL-terminated and mode is a value, not a pointer.
    if unsafe { libc::mkdirat(parent, name.as_ptr(), mode as libc::mode_t) } < 0 {
        return Err(io_error("mkdirat"));
    }
    Ok(())
}

fn unlink_at(parent: RawFd, name: &CString, directory: bool) -> Result<()> {
    let flags = if directory { libc::AT_REMOVEDIR } else { 0 };
    // SAFETY: name is NUL-terminated and the parent fd is retained.
    if unsafe { libc::unlinkat(parent, name.as_ptr(), flags) } < 0 {
        return Err(io_error("unlinkat"));
    }
    Ok(())
}

fn rename_at(
    source_parent: RawFd,
    source: &CString,
    destination_parent: RawFd,
    destination: &CString,
) -> Result<()> {
    // RENAME_NOREPLACE makes a destination replacement impossible. The
    // operation is still anchored to both retained parent descriptors.
    // SAFETY: names are NUL-terminated and all syscall arguments are values.
    let result = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            source_parent,
            source.as_ptr(),
            destination_parent,
            destination.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    };
    if result < 0 {
        return Err(io_error("renameat2"));
    }
    Ok(())
}

fn reopen_directory_fd(fd: RawFd) -> Result<OwnedFd> {
    // Reopening "." through the retained directory descriptor creates a new
    // open-file description.  dup() would share the directory cursor with the
    // caller and make repeated listings consume one another's position.
    let dot = CString::new(".").expect("literal has no NUL");
    let flags = libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW;
    // SAFETY: dot is a NUL-terminated component and fd is retained by a
    // DirCap; the returned descriptor is transferred to OwnedFd.
    let reopened = unsafe { libc::openat(fd, dot.as_ptr(), flags) };
    if reopened < 0 {
        return Err(io_error("openat directory cursor"));
    }
    // SAFETY: reopened is freshly returned and uniquely owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(reopened) })
}

/// RAII owner for the DIR* returned by fdopendir.  In particular, a readdir
/// error must not bypass closedir: NULL is both EOF and error and the error
/// path is otherwise an easy descriptor leak.
struct DirStream {
    raw: *mut libc::DIR,
}

impl DirStream {
    fn new(raw: *mut libc::DIR) -> Self {
        debug_assert!(!raw.is_null());
        Self { raw }
    }

    fn as_raw(&self) -> *mut libc::DIR {
        self.raw
    }

    fn close(mut self) -> Result<()> {
        let raw = self.raw;
        self.raw = ptr::null_mut();
        // SAFETY: raw is the live DIR* owned by this guard.
        if unsafe { libc::closedir(raw) } < 0 {
            return Err(io_error("closedir"));
        }
        Ok(())
    }
}

impl Drop for DirStream {
    fn drop(&mut self) {
        if !self.raw.is_null() {
            // SAFETY: the guard exclusively owns this DIR*.
            unsafe { libc::closedir(self.raw) };
            self.raw = ptr::null_mut();
        }
    }
}

fn read_directory(
    fd: RawFd,
    max_items: usize,
    deadline_ns: u64,
    clock: &mut impl Clock,
) -> Result<Vec<String>> {
    if max_items == 0 {
        return Err(BoundaryError::BudgetExceeded("directory item limit"));
    }
    let reopened = reopen_directory_fd(fd)?;
    let reopened_fd = reopened.into_raw_fd();
    // SAFETY: fdopendir takes ownership of the duplicate descriptor. The
    // Scandir guard below closes the DIR and therefore the duplicate.
    let directory = unsafe { libc::fdopendir(reopened_fd) };
    if directory.is_null() {
        // fdopendir did not take ownership on failure; reconstruct the RAII
        // owner so this error path cannot leak the reopened descriptor.
        // SAFETY: reopened_fd is still uniquely owned here.
        drop(unsafe { OwnedFd::from_raw_fd(reopened_fd) });
        return Err(io_error("fdopendir"));
    }
    let directory = DirStream::new(directory);
    let mut names = Vec::new();
    loop {
        if clock.now_ns() >= deadline_ns {
            return Err(BoundaryError::BudgetExceeded("directory deadline"));
        }
        // POSIX uses NULL for both EOF and errors.  Clear errno before the
        // call so an old unrelated errno cannot turn a clean EOF into a
        // false failure, and fail closed when readdir reports an error.
        unsafe { *libc::__errno_location() = 0 };
        // SAFETY: readdir returns a borrowed entry valid until the next call.
        let entry = unsafe { libc::readdir(directory.as_raw()) };
        if entry.is_null() {
            let errno = unsafe { *libc::__errno_location() };
            readdir_end_or_error(errno)?;
            break;
        }
        // SAFETY: entry is non-null and d_name is a NUL-terminated field.
        let name = unsafe {
            OsStr::from_bytes(std::ffi::CStr::from_ptr((*entry).d_name.as_ptr()).to_bytes())
        }
        .to_string_lossy()
        .into_owned();
        if name == "." || name == ".." {
            continue;
        }
        if names.len() >= max_items {
            return Err(BoundaryError::BudgetExceeded("directory item limit"));
        }
        names.push(name);
    }
    directory.close()?;
    Ok(names)
}

fn readdir_end_or_error(errno: i32) -> Result<()> {
    if errno == 0 {
        Ok(())
    } else {
        Err(BoundaryError::Io {
            operation: "readdir",
            source: std::io::Error::from_raw_os_error(errno),
        })
    }
}

pub struct Boundary<C: Clock, O: Observer, F: FaultInjector> {
    clock: C,
    observer: O,
    faults: F,
    sequence: u64,
    next_id: u64,
    quiescent_scope: Option<QuiescentScope>,
    quiescence_invalidated: bool,
    pending_quarantines: Vec<QuarantineObligation>,
    pending_stages: Vec<StageObligation>,
    rollback_disposition: RollbackDisposition,
}

impl Boundary<RealClock, NoopObserver, NoFault> {
    pub fn production() -> Self {
        Self::new(RealClock::default(), NoopObserver, NoFault)
    }
}

impl<C: Clock, O: Observer, F: FaultInjector> Boundary<C, O, F> {
    pub fn new(clock: C, observer: O, faults: F) -> Self {
        Self {
            clock,
            observer,
            faults,
            sequence: 0,
            next_id: 0,
            quiescent_scope: None,
            quiescence_invalidated: false,
            pending_quarantines: Vec::new(),
            pending_stages: Vec::new(),
            rollback_disposition: RollbackDisposition::ReadyForQuarantine,
        }
    }

    /// Install a writer-stop proof supplied by the lifecycle controller.  A
    /// boundary never derives this scope from its own directory or lock
    /// checks; callers must explicitly hand it over after quiescing writers.
    pub fn set_quiescent_scope(&mut self, scope: QuiescentScope) {
        self.quiescence_invalidated = false;
        self.quiescent_scope = Some(scope);
    }

    /// Return quarantine obligations that could not be collected because an
    /// external quiescence proof was absent or GC failed.  The caller owns
    /// their later recovery/GC decision.
    #[must_use = "quarantine obligations must be handled"]
    pub fn take_quarantine_obligations(&mut self) -> QuarantineObligations {
        QuarantineObligations {
            obligations: std::mem::take(&mut self.pending_quarantines),
        }
    }

    /// Return staging obligations that could not be published or safely
    /// rolled back.  The caller owns the retained capability/recovery decision.
    #[must_use = "stage obligations must be handled"]
    pub fn take_stage_obligations(&mut self) -> StageObligations {
        StageObligations {
            obligations: std::mem::take(&mut self.pending_stages),
        }
    }

    fn next_id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    fn register_unbound_stage(
        &mut self,
        parent: &DirCap,
        stage: StageRef,
        child: Option<ChildRef>,
        name: CString,
        expected: Option<FileIdentity>,
        child_identity: Option<FileIdentity>,
    ) -> usize {
        // The duplicate is best-effort, but the typed obligation is never
        // dropped when duplication fails.  A None parent descriptor is an
        // explicit unbound recovery state, not an implicit cleanup success.
        let parent_fd = duplicate_fd(parent.raw_fd()).ok();
        self.pending_stages
            .push(StageObligation::Unbound(UnboundStageObligation {
                parent_fd,
                parent_identity: parent.identity(),
                stage_ref: stage,
                child,
                name,
                expected,
                child_identity,
                scope: parent.scope(),
            }));
        self.pending_stages.len() - 1
    }

    fn resolve_stage_obligation(&mut self, index: usize) {
        let _ = self.pending_stages.swap_remove(index);
    }

    fn retain_stage_authority(
        &mut self,
        parent: &DirCap,
        stage: &DirCap,
        name: &CString,
        child: Option<ChildRef>,
        child_identity: Option<FileIdentity>,
    ) {
        let stage_ref = StageRef::new(stage.id());
        let parent_fd = duplicate_fd(parent.raw_fd());
        let stage_fd = duplicate_fd(stage.raw_fd());
        match (parent_fd, stage_fd) {
            (Ok(parent_fd), Ok(stage_fd)) => {
                self.pending_stages
                    .push(StageObligation::Bound(BoundStageObligation {
                        parent: DirCap::new(
                            parent_fd,
                            parent.identity(),
                            parent.id(),
                            parent.scope(),
                        ),
                        stage: DirCap::new(stage_fd, stage.identity(), stage.id(), stage.scope()),
                        stage_ref,
                        name: name.clone(),
                        child,
                        child_identity,
                    }))
            }
            _ => {
                self.register_unbound_stage(
                    parent,
                    stage_ref,
                    child,
                    name.clone(),
                    Some(stage.identity()),
                    child_identity,
                );
            }
        }
    }

    fn record_operation(
        &mut self,
        operation: OperationName,
        capabilities: CapabilitySet,
        result: OperationOutcome,
        checkpoint: Option<Checkpoint>,
    ) {
        self.sequence += 1;
        self.observer.record(OperationRecord {
            sequence: self.sequence,
            record: JournalRecord::Operation(OperationEvent {
                operation,
                capabilities,
                result,
                checkpoint,
            }),
        });
    }

    fn record(
        &mut self,
        operation: OperationName,
        capabilities: CapabilitySet,
        outcome: OperationOutcome,
    ) {
        self.record_operation(operation, capabilities, outcome, None);
    }

    fn record_with(
        &mut self,
        operation: OperationName,
        capabilities: CapabilitySet,
        outcome: OperationOutcome,
        checkpoint: Option<Checkpoint>,
    ) {
        self.record_operation(operation, capabilities, outcome, checkpoint);
    }

    fn record_stage(&mut self, event: StageJournal) {
        self.sequence += 1;
        self.observer.record(OperationRecord {
            sequence: self.sequence,
            record: JournalRecord::Stage(event),
        });
    }

    fn record_stage_allocated(&mut self, parent: &DirCap, lease: &ExclusiveLease, stage: StageRef) {
        self.record_stage(StageJournal::Allocated {
            capabilities: CapabilitySet::stage(parent, lease, stage, None),
            stage,
            identity: IdentityObservation::NotApplicable,
        });
    }

    fn record_stage_registered(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        stage: StageRef,
        child: ChildRef,
        identity: IdentityObservation,
    ) {
        self.record_stage(StageJournal::Registered {
            capabilities: CapabilitySet::stage(parent, lease, stage, Some(child)),
            stage,
            child,
            identity,
        });
    }

    fn record_stage_published(
        &mut self,
        source_parent: &DirCap,
        destination_parent: &DirCap,
        lease: &ExclusiveLease,
        stage: StageRef,
        child: ChildRef,
        identity: IdentityObservation,
    ) {
        self.record_stage(StageJournal::Published {
            capabilities: CapabilitySet::publish(
                source_parent,
                destination_parent,
                lease,
                stage,
                child,
            ),
            stage,
            child,
            identity,
        });
    }

    fn record_stage_cleanup(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        stage: StageRef,
        terminal: StageCleanupTerminal,
        identity: IdentityObservation,
    ) {
        self.record_stage(StageJournal::Cleanup {
            capabilities: CapabilitySet::stage(parent, lease, stage, None),
            stage,
            terminal,
            identity,
        });
    }

    fn record_quarantine(
        &mut self,
        object_id: u64,
        terminal: QuarantineTerminal,
        identity: FileIdentity,
    ) {
        let object = QuarantineRef::new(object_id);
        let event = match terminal {
            QuarantineTerminal::Restored => QuarantineEvent::Restored {
                capabilities: CapabilitySet::quarantine(object),
                object,
                identity,
            },
            QuarantineTerminal::BoundRecovery => QuarantineEvent::BoundRecovery {
                capabilities: CapabilitySet::quarantine(object),
                object,
                identity,
            },
            QuarantineTerminal::UnboundRecovery => QuarantineEvent::UnboundRecovery {
                capabilities: CapabilitySet::quarantine(object),
                object,
                identity,
            },
            QuarantineTerminal::Collected => QuarantineEvent::Collected {
                capabilities: CapabilitySet::quarantine(object),
                object,
                identity,
            },
        };
        self.sequence += 1;
        self.observer.record(OperationRecord {
            sequence: self.sequence,
            record: JournalRecord::Quarantine(event),
        });
    }

    fn fail_or_record<T>(
        &mut self,
        operation: OperationName,
        capabilities: CapabilitySet,
        result: Result<T>,
    ) -> Result<T> {
        self.record_operation(
            operation,
            capabilities,
            if result.is_ok() {
                OperationOutcome::Observed
            } else {
                OperationOutcome::Error
            },
            None,
        );
        result
    }

    fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
        // Fault hooks are test/explorer namespace writers, not participants
        // in the lifecycle quiescence proof. Temporarily withdraw the parked
        // scope while invoking either hook. A writer hook permanently
        // invalidates the old proof; dropping the Rust value does not restore
        // the external barrier, so callers must establish a new one.
        let parked_scope = self.quiescent_scope.take();
        let result = self.faults.checkpoint(checkpoint);
        if self.faults.take_quiescence_invalidation() {
            self.quiescence_invalidated = true;
        } else {
            self.quiescent_scope = parked_scope;
        }
        self.record_operation(
            OperationName::Checkpoint,
            CapabilitySet::empty(),
            if result.is_ok() {
                OperationOutcome::Observed
            } else {
                OperationOutcome::Injected
            },
            Some(checkpoint),
        );
        result?;
        let parked_scope = self.quiescent_scope.take();
        let after = self.faults.after_checkpoint(checkpoint);
        if self.faults.take_quiescence_invalidation() {
            self.quiescence_invalidated = true;
        } else {
            self.quiescent_scope = parked_scope;
        }
        if let Err(error) = after {
            self.record_operation(
                OperationName::Checkpoint,
                CapabilitySet::empty(),
                OperationOutcome::Injected,
                Some(checkpoint),
            );
            return Err(error);
        }
        Ok(())
    }

    pub fn now_ns(&mut self) -> u64 {
        let value = self.clock.now_ns();
        self.record_operation(
            OperationName::ClockNow,
            CapabilitySet::empty(),
            OperationOutcome::Observed,
            None,
        );
        value
    }

    pub fn anchor_directory(&mut self, path: &Path) -> Result<DirCap> {
        if !path.is_absolute() {
            return Err(BoundaryError::NotAbsolutePath);
        }
        let scope = fresh_scope();
        let mut current = open_root()?;
        for component in path.components() {
            let component = component.as_os_str();
            if component == OsStr::new("/") {
                continue;
            }
            let name = c_name_bytes(component.as_bytes())?;
            current = open_dir_at(current.as_raw_fd(), &name)?;
        }
        let identity = FileIdentity::from_fd(current.as_raw_fd())?;
        let cap = DirCap::new(current, identity, self.next_id(), scope);
        self.record(
            OperationName::AnchorDirectory,
            CapabilitySet::anchor(&cap),
            OperationOutcome::Observed,
        );
        Ok(cap)
    }

    pub fn revalidate_directory(
        &mut self,
        capability: &DirCap,
        metadata: bool,
    ) -> Result<FileIdentity> {
        let observed = FileIdentity::from_fd(capability.raw_fd())?;
        let result = if observed.inode_key() != capability.identity().inode_key()
            || (metadata && observed != capability.identity())
        {
            Err(BoundaryError::IdentityMismatch)
        } else {
            Ok(observed)
        };
        self.fail_or_record(
            if metadata {
                OperationName::RevalidateMetadata
            } else {
                OperationName::Revalidate
            },
            CapabilitySet::directory(capability),
            result,
        )
    }

    pub fn open_file(&mut self, parent: &DirCap, name: &str, writable: bool) -> Result<FileCap> {
        self.revalidate_directory(parent, false)?;
        let c_name = c_name(name)?;
        let fd = open_file_at(parent.raw_fd(), &c_name, writable)?;
        let identity = FileIdentity::from_fd(fd.as_raw_fd())?;
        let cap = FileCap::new(fd, identity, self.next_id(), parent.scope());
        self.record(
            OperationName::OpenChild,
            CapabilitySet::open_file(parent, &cap),
            OperationOutcome::Observed,
        );
        Ok(cap)
    }

    pub fn open_lock(&mut self, parent: &DirCap, name: &str) -> Result<LockCap> {
        self.revalidate_directory(parent, false)?;
        let c_name = c_name(name)?;
        let fd = open_file_at(parent.raw_fd(), &c_name, true)?;
        let identity = FileIdentity::from_fd(fd.as_raw_fd())?;
        if identity.mode & libc::S_IFMT != libc::S_IFREG {
            return Err(BoundaryError::WrongCapability(
                "lock must be a regular file",
            ));
        }
        let cap = LockCap::new(fd, identity, self.next_id(), parent.scope());
        self.record(
            OperationName::OpenLock,
            CapabilitySet::open_lock(parent, &cap),
            OperationOutcome::Observed,
        );
        Ok(cap)
    }

    pub fn open_directory(&mut self, parent: &DirCap, name: &str) -> Result<DirCap> {
        self.revalidate_directory(parent, false)?;
        let c_name = c_name(name)?;
        let fd = open_dir_at(parent.raw_fd(), &c_name)?;
        let identity = FileIdentity::from_fd(fd.as_raw_fd())?;
        let cap = DirCap::new(fd, identity, self.next_id(), parent.scope());
        self.record(
            OperationName::OpenChild,
            CapabilitySet::open_directory(parent, &cap),
            OperationOutcome::Observed,
        );
        Ok(cap)
    }

    pub fn mkdir_child(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &str,
        mode: u32,
    ) -> Result<DirCap> {
        self.revalidate_directory(parent, false)?;
        self.revalidate_lock(lease)?;
        self.require_scope(parent.scope(), lease.scope())?;
        let final_name = c_name(name)?;
        let (stage_name, stage) = match self.create_staging_dir(parent, lease) {
            Ok(value) => value,
            Err(error) => {
                self.record_with(
                    OperationName::MkdirChild,
                    CapabilitySet::mkdir_start(parent, lease),
                    OperationOutcome::StagedError,
                    None,
                );
                return Err(error);
            }
        };
        let staged_name = CString::new("entry").expect("literal has no NUL");
        let stage_ref = StageRef::new(stage.id());
        if let Err(error) = mkdir_at(stage.raw_fd(), &staged_name, mode) {
            let cleanup = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), None),
                if cleanup.is_ok() {
                    OperationOutcome::StagedError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(error);
        }
        let child_id = self.next_id();
        let child_ref = ChildRef::new(child_id);
        let child_stage_ref = StageRef::new(child_id);
        self.record_stage_allocated(&stage, lease, child_stage_ref);
        let child_identity = match FileIdentity::from_at(stage.raw_fd(), &staged_name) {
            Ok(identity) => identity,
            Err(error) => {
                // The child stage is already allocated, but its identity could
                // not be bound.  A typed deferred terminal is mandatory here:
                // recovery must see the unbound stage rather than infer it from
                // a missing Registered record.
                let obligation = self.register_unbound_stage(
                    &stage,
                    child_stage_ref,
                    Some(child_ref),
                    staged_name.clone(),
                    None,
                    None,
                );
                let cleanup_stage =
                    self.cleanup_registered(parent, lease, &stage_name, &stage, true);
                if cleanup_stage.is_ok() {
                    self.resolve_stage_obligation(obligation);
                }
                self.record_stage_cleanup(
                    &stage,
                    lease,
                    child_stage_ref,
                    if cleanup_stage.is_ok() {
                        StageCleanupTerminal::Cleaned
                    } else {
                        StageCleanupTerminal::Deferred
                    },
                    IdentityObservation::NotApplicable,
                );
                self.record_with(
                    OperationName::MkdirChild,
                    CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), None),
                    if cleanup_stage.is_ok() {
                        OperationOutcome::StagedError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(error);
            }
        };
        self.record_stage_registered(
            &stage,
            lease,
            child_stage_ref,
            child_ref,
            IdentityObservation::Observed(child_identity),
        );
        if let Err(error) = self.checkpoint(Checkpoint::AfterMkdirBeforeBind) {
            let cleanup_child = self.cleanup_registered_identity(
                &stage,
                lease,
                &staged_name,
                child_identity,
                child_id,
            );
            let cleanup_stage = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(error);
        }
        let fd = match open_dir_at(stage.raw_fd(), &staged_name) {
            Ok(fd) => fd,
            Err(error) => {
                let cleanup_child = self.cleanup_registered_identity(
                    &stage,
                    lease,
                    &staged_name,
                    child_identity,
                    child_id,
                );
                let cleanup_stage =
                    self.cleanup_registered(parent, lease, &stage_name, &stage, true);
                self.record_with(
                    OperationName::MkdirChild,
                    CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                    if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                        OperationOutcome::StagedError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(error);
            }
        };
        let identity = match FileIdentity::from_fd(fd.as_raw_fd()) {
            Ok(identity) => identity,
            Err(error) => {
                let cleanup_child = self.cleanup_registered_identity(
                    &stage,
                    lease,
                    &staged_name,
                    child_identity,
                    child_id,
                );
                let cleanup_stage =
                    self.cleanup_registered(parent, lease, &stage_name, &stage, true);
                self.record_with(
                    OperationName::MkdirChild,
                    CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                    if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                        OperationOutcome::StagedError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(error);
            }
        };
        if identity.inode_key() != child_identity.inode_key() {
            let cleanup_child = self.cleanup_registered_identity(
                &stage,
                lease,
                &staged_name,
                child_identity,
                child_id,
            );
            let cleanup_stage = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    OperationOutcome::IdentityMismatch
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(BoundaryError::IdentityMismatch);
        }
        let cap = DirCap::new(fd, identity, child_id, stage.scope());
        if let Err(error) = self.checkpoint(Checkpoint::AfterBindBeforePublish) {
            let cleanup_child = self.cleanup_registered(&stage, lease, &staged_name, &cap, true);
            let cleanup_stage = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(error);
        }
        if let Err(error) = rename_at(stage.raw_fd(), &staged_name, parent.raw_fd(), &final_name) {
            let cleanup_child = self.cleanup_registered(&stage, lease, &staged_name, &cap, true);
            let cleanup_stage = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(error);
        }
        self.record_stage_published(
            &stage,
            parent,
            lease,
            child_stage_ref,
            child_ref,
            IdentityObservation::Observed(identity),
        );
        if let Err(error) = self.cleanup_registered(parent, lease, &stage_name, &stage, true) {
            self.record_with(
                OperationName::MkdirChild,
                CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
                OperationOutcome::MutatedError,
                None,
            );
            return Err(error);
        }
        self.record_with(
            OperationName::MkdirChild,
            CapabilitySet::mkdir_child(parent, lease, Some(stage_ref), Some(child_ref)),
            OperationOutcome::Applied,
            None,
        );
        Ok(cap)
    }

    fn revalidate_lock(&mut self, lease: &ExclusiveLease) -> Result<()> {
        let observed = FileIdentity::from_fd(lease.raw_fd())?;
        if observed.inode_key() != lease.identity().inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        Ok(())
    }

    fn require_scope(&self, expected: ScopeId, actual: ScopeId) -> Result<()> {
        if expected == actual {
            Ok(())
        } else {
            Err(BoundaryError::WrongCapability("authority scope"))
        }
    }

    /// Final exact mutation is name-based at the kernel ABI, so the retained
    /// object identity alone cannot close the last unlinkat/renameat window.
    /// Require the lifecycle controller's external writer-stop proof and
    /// validate that its anchored parent/lease still match immediately before
    /// the syscall.  Without this capability the operation leaves its staged
    /// object as an explicit recovery obligation instead of guessing.
    fn require_exact_mutation_scope(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
    ) -> Result<()> {
        let Some(scope) = self.quiescent_scope.as_ref() else {
            return Err(BoundaryError::QuarantineRequired);
        };
        let scope_id = scope.scope;
        let scope_parent_identity = scope.parent_identity;
        let scope_secondary_parent_identity = scope.secondary_parent_identity;
        let scope_secondary_parent_scope = scope.secondary_parent_scope;
        let scope_lease_identity = scope.lease_identity;
        if parent.scope() != scope_id && scope_secondary_parent_scope != Some(parent.scope()) {
            return Err(BoundaryError::WrongCapability("authority scope"));
        }
        let observed_parent = self.revalidate_directory(parent, false)?;
        let observed_inode = observed_parent.inode_key();
        if scope_parent_identity.inode_key() != observed_inode
            && scope_secondary_parent_identity
                .is_none_or(|secondary| secondary.inode_key() != observed_inode)
        {
            return Err(BoundaryError::IdentityMismatch);
        }
        if FileIdentity::from_fd(lease.raw_fd())?.inode_key() != scope_lease_identity.inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        Ok(())
    }

    /// Collect a quarantined object only with the externally established
    /// writer-stop scope.  The identity check is intentionally made against
    /// the retained transaction object, not a caller-provided bare name.
    /// Once the scope is installed, no namespace writer is allowed to run;
    /// any observed replacement still fails closed before unlink.
    pub fn gc_quarantine(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        scope: &QuiescentScope,
        object: &QuarantinedObject,
    ) -> Result<()> {
        self.require_scope(parent.scope(), scope.scope)?;
        self.require_scope(parent.scope(), object.scope)?;
        let observed_parent = self.revalidate_directory(parent, false)?;
        if !scope.allows_parent(observed_parent)
            || observed_parent.inode_key() != object.parent_identity.inode_key()
        {
            return Err(BoundaryError::IdentityMismatch);
        }
        self.revalidate_lock(lease)?;
        if FileIdentity::from_fd(lease.raw_fd())?.inode_key() != scope.lease_identity.inode_key()
            || FileIdentity::from_fd(lease.raw_fd())?.inode_key()
                != object.lease_identity.inode_key()
        {
            return Err(BoundaryError::IdentityMismatch);
        }
        if FileIdentity::from_fd(object.fd.as_raw_fd())?.inode_key() != object.expected.inode_key()
        {
            return Err(BoundaryError::IdentityMismatch);
        }
        let observed = FileIdentity::from_at(parent.raw_fd(), &object.quarantine_name)?;
        if observed.inode_key() != object.expected.inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        // This checkpoint is after the final successful identity observation
        // and immediately before the sole unlink.  A test-only replacement at
        // this point is treated as a violated external quiescence claim and
        // fails closed; production callers must not run writers in this scope.
        self.checkpoint(Checkpoint::AfterQuarantineVerifyBeforeGc)?;
        if self.quiescence_invalidated {
            return Err(BoundaryError::QuarantineRequired);
        }
        if FileIdentity::from_fd(object.fd.as_raw_fd())?.inode_key() != object.expected.inode_key()
            || FileIdentity::from_at(parent.raw_fd(), &object.quarantine_name)?.inode_key()
                != object.expected.inode_key()
        {
            return Err(BoundaryError::IdentityMismatch);
        }
        unlink_at(parent.raw_fd(), &object.quarantine_name, object.directory)
    }

    fn rollback_created(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &CString,
        cap: &DirCap,
        directory: bool,
    ) -> Result<QuarantinedObject> {
        self.rollback_disposition = RollbackDisposition::PreQuarantineFailure;
        self.require_scope(parent.scope(), lease.scope())?;
        self.require_scope(parent.scope(), cap.scope())?;
        self.rollback_named(parent, lease, name, cap.identity(), directory)
    }

    fn finish_quarantine(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        object: QuarantinedObject,
    ) -> Result<()> {
        let Some(scope) = self.quiescent_scope.take() else {
            let object_id = object.id();
            let identity = object.identity();
            self.pending_quarantines
                .push(QuarantineObligation::Bound(object));
            self.record_quarantine(object_id, QuarantineTerminal::BoundRecovery, identity);
            return Err(BoundaryError::QuarantineRequired);
        };
        let object_id = object.id();
        let identity = object.identity();
        let result = self.gc_quarantine(parent, lease, &scope, &object);
        if !self.quiescence_invalidated {
            self.quiescent_scope = Some(scope);
        }
        if result.is_err() {
            self.pending_quarantines
                .push(QuarantineObligation::Bound(object));
            self.record_quarantine(object_id, QuarantineTerminal::BoundRecovery, identity);
        } else {
            self.record_quarantine(object_id, QuarantineTerminal::Collected, identity);
        }
        result
    }

    /// Finish a registered staging resource exactly once.  The journal event
    /// is emitted on both success and failure, so recovery never has to infer
    /// whether a registered resource was cleaned merely because a syscall
    /// returned an error.
    fn cleanup_registered(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &CString,
        cap: &DirCap,
        directory: bool,
    ) -> Result<()> {
        let result = match self.rollback_created(parent, lease, name, cap, directory) {
            Ok(object) => self.finish_quarantine(parent, lease, object),
            Err(error) => {
                if self.rollback_disposition.retains_stage_authority() {
                    self.retain_stage_authority(parent, cap, name, None, None);
                }
                Err(error)
            }
        };
        self.record_stage_cleanup(
            parent,
            lease,
            StageRef::new(cap.id()),
            if result.is_ok() {
                StageCleanupTerminal::Cleaned
            } else {
                StageCleanupTerminal::Failed
            },
            IdentityObservation::Observed(cap.identity()),
        );
        result
    }

    fn cleanup_registered_identity(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &CString,
        expected: FileIdentity,
        capability_id: u64,
    ) -> Result<()> {
        let result = if let Err(error) = self.require_scope(parent.scope(), lease.scope()) {
            self.rollback_disposition = RollbackDisposition::PreQuarantineFailure;
            self.register_unbound_stage(
                parent,
                StageRef::new(capability_id),
                None,
                name.clone(),
                Some(expected),
                Some(expected),
            );
            Err(error)
        } else {
            match self.rollback_named(parent, lease, name, expected, true) {
                Ok(object) => self.finish_quarantine(parent, lease, object),
                Err(error) => {
                    if self.rollback_disposition.retains_stage_authority() {
                        self.register_unbound_stage(
                            parent,
                            StageRef::new(capability_id),
                            None,
                            name.clone(),
                            Some(expected),
                            Some(expected),
                        );
                    }
                    Err(error)
                }
            }
        };
        self.record_stage_cleanup(
            parent,
            lease,
            StageRef::new(capability_id),
            if result.is_ok() {
                StageCleanupTerminal::Cleaned
            } else {
                StageCleanupTerminal::Failed
            },
            IdentityObservation::Observed(expected),
        );
        result
    }

    fn rollback_named_and_finish_unbound_stage(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &CString,
        stage: StageRef,
        expected: FileIdentity,
    ) -> Result<()> {
        match self.rollback_named(parent, lease, name, expected, true) {
            Ok(object) => self.finish_quarantine(parent, lease, object),
            Err(error) => {
                if self.rollback_disposition.retains_stage_authority() {
                    self.register_unbound_stage(
                        parent,
                        stage,
                        Some(ChildRef::new(stage.id())),
                        name.clone(),
                        Some(expected),
                        None,
                    );
                }
                Err(error)
            }
        }
    }

    fn restore_quarantine(
        parent: &DirCap,
        quarantine_name: &CString,
        original_name: &CString,
        expected: FileIdentity,
    ) -> Result<()> {
        rename_at(
            parent.raw_fd(),
            quarantine_name,
            parent.raw_fd(),
            original_name,
        )?;
        let restored = FileIdentity::from_at(parent.raw_fd(), original_name)?;
        if restored.inode_key() != expected.inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        Ok(())
    }

    fn restore_or_track_quarantine(
        &mut self,
        parent: &DirCap,
        original_name: &CString,
        restore_expected: FileIdentity,
        unbound: UnboundQuarantine,
        bound: Option<QuarantinedObject>,
        error: BoundaryError,
    ) -> BoundaryError {
        if Self::restore_quarantine(
            parent,
            &unbound.quarantine_name,
            original_name,
            restore_expected,
        )
        .is_ok()
        {
            self.rollback_disposition = RollbackDisposition::RestoredAfterMove;
            self.record_quarantine(unbound.id, QuarantineTerminal::Restored, restore_expected);
            return error;
        }
        let (obligation, terminal, identity) = match bound {
            Some(object) => {
                let identity = object.identity();
                (
                    QuarantineObligation::Bound(object),
                    QuarantineTerminal::BoundRecovery,
                    identity,
                )
            }
            None => (
                QuarantineObligation::Unbound(unbound),
                QuarantineTerminal::UnboundRecovery,
                restore_expected,
            ),
        };
        let object_id = obligation.id();
        self.rollback_disposition = RollbackDisposition::QuarantinedForRecovery;
        self.pending_quarantines.push(obligation);
        self.record_quarantine(object_id, terminal, identity);
        BoundaryError::QuarantineRequired
    }

    fn rollback_named(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &CString,
        expected: FileIdentity,
        directory: bool,
    ) -> Result<QuarantinedObject> {
        self.rollback_disposition = RollbackDisposition::PreQuarantineFailure;
        self.require_scope(parent.scope(), lease.scope())?;
        self.revalidate_directory(parent, false)?;
        self.revalidate_lock(lease)?;
        // Move the currently named inode into a transaction-private name
        // before inspecting or deleting it.  A check-then-unlink sequence is
        // not exact: a replacement can occupy `name` between the two calls.
        // The quarantine move makes the object being inspected the only object
        // that can subsequently be removed.  RENAME_NOREPLACE also ensures a
        // concurrent replacement is never overwritten during restoration.
        let quarantine_name = CString::new(format!(".lifecycle-quarantine-{}", self.next_id()))
            .expect("quarantine name has no NUL");
        rename_at(parent.raw_fd(), name, parent.raw_fd(), &quarantine_name)?;
        let unbound = UnboundQuarantine {
            parent_fd: duplicate_fd(parent.raw_fd()).ok(),
            parent_identity: parent.identity(),
            lease_identity: lease.identity(),
            scope: parent.scope(),
            quarantine_name: quarantine_name.clone(),
            expected,
            directory,
            id: self.next_id(),
        };

        let fd = match open_object_path_at(parent.raw_fd(), &quarantine_name) {
            Ok(fd) => fd,
            Err(error) => {
                return Err(
                    self.restore_or_track_quarantine(parent, name, expected, unbound, None, error)
                );
            }
        };
        let parent_fd = match duplicate_fd(parent.raw_fd()) {
            Ok(fd) => fd,
            Err(error) => {
                return Err(
                    self.restore_or_track_quarantine(parent, name, expected, unbound, None, error)
                );
            }
        };
        let object = QuarantinedObject {
            fd,
            parent: DirCap::new(parent_fd, parent.identity(), parent.id(), parent.scope()),
            parent_identity: parent.identity(),
            lease_identity: lease.identity(),
            scope: parent.scope(),
            quarantine_name,
            expected,
            directory,
            id: unbound.id,
        };
        if let Err(error) = self.checkpoint(Checkpoint::AfterQuarantineMoveBeforeVerify) {
            return Err(self.restore_or_track_quarantine(
                parent,
                name,
                expected,
                unbound,
                Some(object),
                error,
            ));
        }
        let moved = match FileIdentity::from_fd(object.fd.as_raw_fd()) {
            Ok(identity) => identity,
            Err(error) => {
                return Err(self.restore_or_track_quarantine(
                    parent,
                    name,
                    expected,
                    unbound,
                    Some(object),
                    error,
                ));
            }
        };
        if moved.inode_key() != expected.inode_key() {
            return Err(self.restore_or_track_quarantine(
                parent,
                name,
                moved,
                unbound,
                Some(object),
                BoundaryError::IdentityMismatch,
            ));
        }
        match FileIdentity::from_at(parent.raw_fd(), name) {
            Ok(_) => {
                // A replacement appeared while the expected inode was in
                // quarantine.  Leave both objects intact for recovery rather
                // than claiming a clean stage disposal.
                let object_id = object.id();
                let identity = object.identity();
                self.rollback_disposition = RollbackDisposition::QuarantinedForRecovery;
                self.pending_quarantines
                    .push(QuarantineObligation::Bound(object));
                self.record_quarantine(object_id, QuarantineTerminal::BoundRecovery, identity);
                return Err(BoundaryError::IdentityMismatch);
            }
            Err(BoundaryError::Io { source, .. })
                if source.raw_os_error() == Some(libc::ENOENT) => {}
            Err(error) => {
                let object_id = object.id();
                let identity = object.identity();
                self.rollback_disposition = RollbackDisposition::QuarantinedForRecovery;
                self.pending_quarantines
                    .push(QuarantineObligation::Bound(object));
                self.record_quarantine(object_id, QuarantineTerminal::BoundRecovery, identity);
                return Err(error);
            }
        }
        self.rollback_disposition = RollbackDisposition::ReadyForQuarantine;
        Ok(object)
    }

    fn create_staging_dir(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
    ) -> Result<(CString, DirCap)> {
        self.revalidate_directory(parent, false)?;
        self.revalidate_lock(lease)?;
        self.require_scope(parent.scope(), lease.scope())?;
        let stage_id = self.next_id();
        let stage_name =
            CString::new(format!(".lifecycle-stage-{stage_id}")).expect("stage name has no NUL");
        mkdir_at(parent.raw_fd(), &stage_name, 0o700)?;
        let stage_ref = StageRef::new(stage_id);
        self.record_stage_allocated(parent, lease, stage_ref);
        let identity = match FileIdentity::from_at(parent.raw_fd(), &stage_name) {
            Ok(identity) => identity,
            Err(error) => {
                self.register_unbound_stage(
                    parent,
                    stage_ref,
                    Some(ChildRef::new(stage_id)),
                    stage_name.clone(),
                    None,
                    None,
                );
                self.record_stage_cleanup(
                    parent,
                    lease,
                    stage_ref,
                    StageCleanupTerminal::Deferred,
                    IdentityObservation::NotApplicable,
                );
                return Err(error);
            }
        };
        self.record_stage_registered(
            parent,
            lease,
            stage_ref,
            ChildRef::new(stage_id),
            IdentityObservation::Observed(identity),
        );
        // Before the child is bound, fail closed.  There is deliberately no
        // name-only rollback because a replacement could now occupy the name.
        if let Err(error) = self.checkpoint(Checkpoint::AfterStageMkdirBeforeBind) {
            let cleanup = self.rollback_named_and_finish_unbound_stage(
                parent,
                lease,
                &stage_name,
                stage_ref,
                identity,
            );
            self.record_stage_cleanup(
                parent,
                lease,
                stage_ref,
                if cleanup.is_ok() {
                    StageCleanupTerminal::Cleaned
                } else {
                    StageCleanupTerminal::Failed
                },
                IdentityObservation::Observed(identity),
            );
            return Err(error);
        }
        let fd = match open_dir_at(parent.raw_fd(), &stage_name) {
            Ok(fd) => fd,
            Err(error) => {
                let cleanup = self.rollback_named_and_finish_unbound_stage(
                    parent,
                    lease,
                    &stage_name,
                    stage_ref,
                    identity,
                );
                self.record_stage_cleanup(
                    parent,
                    lease,
                    stage_ref,
                    if cleanup.is_ok() {
                        StageCleanupTerminal::Cleaned
                    } else {
                        StageCleanupTerminal::Failed
                    },
                    IdentityObservation::Observed(identity),
                );
                return Err(error);
            }
        };
        let bound_identity = match FileIdentity::from_fd(fd.as_raw_fd()) {
            Ok(identity) => identity,
            Err(error) => {
                let cleanup = self.rollback_named_and_finish_unbound_stage(
                    parent,
                    lease,
                    &stage_name,
                    stage_ref,
                    identity,
                );
                self.record_stage_cleanup(
                    parent,
                    lease,
                    stage_ref,
                    if cleanup.is_ok() {
                        StageCleanupTerminal::Cleaned
                    } else {
                        StageCleanupTerminal::Failed
                    },
                    IdentityObservation::Observed(identity),
                );
                return Err(error);
            }
        };
        if bound_identity.inode_key() != identity.inode_key() {
            let cleanup = self.rollback_named_and_finish_unbound_stage(
                parent,
                lease,
                &stage_name,
                stage_ref,
                identity,
            );
            self.record_stage_cleanup(
                parent,
                lease,
                stage_ref,
                if cleanup.is_ok() {
                    StageCleanupTerminal::Cleaned
                } else {
                    StageCleanupTerminal::Failed
                },
                IdentityObservation::Observed(identity),
            );
            return Err(BoundaryError::IdentityMismatch);
        }
        let cap = DirCap::new(fd, bound_identity, stage_id, parent.scope());
        if let Err(error) = self.checkpoint(Checkpoint::AfterStageBindBeforeMove) {
            let cleanup = self.cleanup_registered(parent, lease, &stage_name, &cap, true);
            cleanup?;
            return Err(error);
        }
        Ok((stage_name, cap))
    }

    fn restore_staged(
        &mut self,
        stage: &DirCap,
        parent: &DirCap,
        staged_name: &CString,
        original_name: &CString,
    ) -> Result<()> {
        self.require_scope(stage.scope(), parent.scope())?;
        self.revalidate_directory(parent, false)?;
        let staged = FileIdentity::from_at(stage.raw_fd(), staged_name)?;
        // RENAME_NOREPLACE restores the moved inode only when the original
        // name is still absent.  A foreign replacement is never overwritten.
        rename_at(stage.raw_fd(), staged_name, parent.raw_fd(), original_name)?;
        let restored = FileIdentity::from_at(parent.raw_fd(), original_name)?;
        if restored.inode_key() != staged.inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        Ok(())
    }

    fn restore_staged_and_cleanup(
        &mut self,
        stage: &DirCap,
        parent: &DirCap,
        lease: &ExclusiveLease,
        stage_name: &CString,
        staged_name: &CString,
        original_name: &CString,
    ) -> Result<()> {
        if let Err(error) = self.restore_staged(stage, parent, staged_name, original_name) {
            // The stage still owns the retained object when restoration fails.
            // Transfer that authority before returning the error; otherwise a
            // failed rollback would leave an untracked inode in the stage.
            let child_identity = FileIdentity::from_at(stage.raw_fd(), staged_name).ok();
            self.retain_stage_authority(parent, stage, stage_name, None, child_identity);
            return Err(error);
        }
        self.cleanup_registered(parent, lease, stage_name, stage, true)
    }

    fn exact_identity(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &CString,
        expected: FileIdentity,
    ) -> Result<()> {
        self.require_scope(parent.scope(), lease.scope())?;
        self.revalidate_directory(parent, false)?;
        self.revalidate_lock(lease)?;
        if FileIdentity::from_at(parent.raw_fd(), name)?.inode_key() != expected.inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        self.checkpoint(Checkpoint::BeforeExactMutation)?;
        Ok(())
    }

    pub fn unlink_exact(
        &mut self,
        parent: &DirCap,
        lease: &ExclusiveLease,
        name: &str,
        expected: FileIdentity,
        directory: bool,
    ) -> Result<()> {
        let name = c_name(name)?;
        let capabilities = CapabilitySet::unlink(parent, lease);
        if let Err(error) = self.exact_identity(parent, lease, &name, expected) {
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                OperationOutcome::Error,
                None,
            );
            return Err(error);
        }
        let staged_name = CString::new("entry").expect("literal has no NUL");
        let (stage_name, stage) = match self.create_staging_dir(parent, lease) {
            Ok(value) => value,
            Err(error) => {
                self.record_with(
                    OperationName::UnlinkExact,
                    capabilities.clone(),
                    OperationOutcome::StagedError,
                    None,
                );
                return Err(error);
            }
        };
        let moved = rename_at(parent.raw_fd(), &name, stage.raw_fd(), &staged_name);
        if let Err(error) = moved {
            let cleanup = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                if cleanup.is_ok() {
                    OperationOutcome::StagedError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(error);
        }
        if let Err(error) = self.checkpoint(Checkpoint::AfterMoveBeforeVerify) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                parent,
                lease,
                &stage_name,
                &staged_name,
                &name,
            );
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        let observed = match FileIdentity::from_at(stage.raw_fd(), &staged_name) {
            Ok(identity) => identity,
            Err(error) => {
                let rollback = self.restore_staged_and_cleanup(
                    &stage,
                    parent,
                    lease,
                    &stage_name,
                    &staged_name,
                    &name,
                );
                self.record_with(
                    OperationName::UnlinkExact,
                    capabilities.clone(),
                    if rollback.is_ok() {
                        OperationOutcome::RolledBackError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(rollback.err().unwrap_or(error));
            }
        };
        if observed.inode_key() != expected.inode_key() {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                parent,
                lease,
                &stage_name,
                &staged_name,
                &name,
            );
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::IdentityMismatch
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(BoundaryError::IdentityMismatch));
        }
        if let Err(error) = self.require_exact_mutation_scope(parent, lease) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                parent,
                lease,
                &stage_name,
                &staged_name,
                &name,
            );
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        // The scope is the external writer-stop proof for the final
        // name-based mutation.  Re-observe the retained stage immediately
        // before unlinkat; a mismatch is handed to the existing typed
        // recovery path and never mutates a foreign inode.
        let final_identity = match FileIdentity::from_at(stage.raw_fd(), &staged_name) {
            Ok(identity) => identity,
            Err(error) => {
                let rollback = self.restore_staged_and_cleanup(
                    &stage,
                    parent,
                    lease,
                    &stage_name,
                    &staged_name,
                    &name,
                );
                self.record_with(
                    OperationName::UnlinkExact,
                    capabilities.clone(),
                    if rollback.is_ok() {
                        OperationOutcome::RolledBackError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(rollback.err().unwrap_or(error));
            }
        };
        if final_identity.inode_key() != expected.inode_key() {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                parent,
                lease,
                &stage_name,
                &staged_name,
                &name,
            );
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::IdentityMismatch
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(BoundaryError::IdentityMismatch));
        }
        if let Err(error) = unlink_at(stage.raw_fd(), &staged_name, directory) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                parent,
                lease,
                &stage_name,
                &staged_name,
                &name,
            );
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        if let Err(error) = self.cleanup_registered(parent, lease, &stage_name, &stage, true) {
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                OperationOutcome::MutatedError,
                None,
            );
            return Err(error);
        }
        if let Err(error) = self.checkpoint(Checkpoint::AfterExactMutation) {
            self.record_with(
                OperationName::UnlinkExact,
                capabilities.clone(),
                OperationOutcome::MutatedError,
                None,
            );
            return Err(error);
        }
        self.record_with(
            OperationName::UnlinkExact,
            capabilities,
            OperationOutcome::Applied,
            None,
        );
        Ok(())
    }

    pub fn rename_exact(
        &mut self,
        source_parent: &DirCap,
        destination_parent: &DirCap,
        lease: &ExclusiveLease,
        source: &str,
        destination: &str,
        expected: FileIdentity,
    ) -> Result<()> {
        let source = c_name(source)?;
        let destination = c_name(destination)?;
        let capabilities = CapabilitySet::rename(source_parent, destination_parent, lease);
        if let Err(error) = self.require_scope(source_parent.scope(), lease.scope()) {
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                OperationOutcome::Error,
                None,
            );
            return Err(error);
        }
        if let Err(error) = self.exact_identity(source_parent, lease, &source, expected) {
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                OperationOutcome::Error,
                None,
            );
            return Err(error);
        }
        if let Err(error) = self.revalidate_directory(destination_parent, false) {
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                OperationOutcome::Error,
                None,
            );
            return Err(error);
        }
        let staged_name = CString::new("entry").expect("literal has no NUL");
        let (stage_name, stage) = match self.create_staging_dir(source_parent, lease) {
            Ok(value) => value,
            Err(error) => {
                self.record_with(
                    OperationName::RenameExact,
                    capabilities.clone(),
                    OperationOutcome::StagedError,
                    None,
                );
                return Err(error);
            }
        };
        if let Err(error) = rename_at(
            source_parent.raw_fd(),
            &source,
            stage.raw_fd(),
            &staged_name,
        ) {
            let cleanup = self.cleanup_registered(source_parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if cleanup.is_ok() {
                    OperationOutcome::StagedError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(error);
        }
        if let Err(error) = self.checkpoint(Checkpoint::AfterMoveBeforeVerify) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                source_parent,
                lease,
                &stage_name,
                &staged_name,
                &source,
            );
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        let observed = match FileIdentity::from_at(stage.raw_fd(), &staged_name) {
            Ok(identity) => identity,
            Err(error) => {
                let rollback = self.restore_staged_and_cleanup(
                    &stage,
                    source_parent,
                    lease,
                    &stage_name,
                    &staged_name,
                    &source,
                );
                self.record_with(
                    OperationName::RenameExact,
                    capabilities.clone(),
                    if rollback.is_ok() {
                        OperationOutcome::RolledBackError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(rollback.err().unwrap_or(error));
            }
        };
        if observed.inode_key() != expected.inode_key() {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                source_parent,
                lease,
                &stage_name,
                &staged_name,
                &source,
            );
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::IdentityMismatch
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(BoundaryError::IdentityMismatch));
        }
        if let Err(error) = self.require_exact_mutation_scope(source_parent, lease) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                source_parent,
                lease,
                &stage_name,
                &staged_name,
                &source,
            );
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        if let Err(error) = self.require_exact_mutation_scope(destination_parent, lease) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                source_parent,
                lease,
                &stage_name,
                &staged_name,
                &source,
            );
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::Error
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        let final_identity = match FileIdentity::from_at(stage.raw_fd(), &staged_name) {
            Ok(identity) => identity,
            Err(error) => {
                let rollback = self.restore_staged_and_cleanup(
                    &stage,
                    source_parent,
                    lease,
                    &stage_name,
                    &staged_name,
                    &source,
                );
                self.record_with(
                    OperationName::RenameExact,
                    capabilities.clone(),
                    if rollback.is_ok() {
                        OperationOutcome::RolledBackError
                    } else {
                        OperationOutcome::RollbackIncomplete
                    },
                    None,
                );
                return Err(rollback.err().unwrap_or(error));
            }
        };
        if final_identity.inode_key() != expected.inode_key() {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                source_parent,
                lease,
                &stage_name,
                &staged_name,
                &source,
            );
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::IdentityMismatch
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(BoundaryError::IdentityMismatch));
        }
        if let Err(error) = rename_at(
            stage.raw_fd(),
            &staged_name,
            destination_parent.raw_fd(),
            &destination,
        ) {
            let rollback = self.restore_staged_and_cleanup(
                &stage,
                source_parent,
                lease,
                &stage_name,
                &staged_name,
                &source,
            );
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                if rollback.is_ok() {
                    OperationOutcome::RolledBackError
                } else {
                    OperationOutcome::RollbackIncomplete
                },
                None,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        if let Err(error) = self.cleanup_registered(source_parent, lease, &stage_name, &stage, true)
        {
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                OperationOutcome::MutatedError,
                None,
            );
            return Err(error);
        }
        if let Err(error) = self.checkpoint(Checkpoint::AfterExactMutation) {
            self.record_with(
                OperationName::RenameExact,
                capabilities.clone(),
                OperationOutcome::MutatedError,
                None,
            );
            return Err(error);
        }
        self.record_with(
            OperationName::RenameExact,
            capabilities,
            OperationOutcome::Applied,
            None,
        );
        Ok(())
    }

    pub fn list_names(
        &mut self,
        parent: &DirCap,
        max_items: usize,
        deadline_ns: u64,
    ) -> Result<Vec<String>> {
        self.revalidate_directory(parent, false)?;
        let result = read_directory(parent.raw_fd(), max_items, deadline_ns, &mut self.clock);
        self.fail_or_record(
            OperationName::ListNames,
            CapabilitySet::directory(parent),
            result,
        )
    }

    pub fn read(&mut self, file: &FileCap, max_bytes: usize) -> Result<Vec<u8>> {
        self.revalidate_file(file)?;
        let mut buffer = vec![0u8; max_bytes];
        // SAFETY: buffer is valid for max_bytes and fd is retained by FileCap.
        let count = unsafe {
            libc::read(
                file.raw_fd(),
                buffer.as_mut_ptr().cast::<c_void>(),
                buffer.len(),
            )
        };
        if count < 0 {
            return Err(io_error("read"));
        }
        buffer.truncate(count as usize);
        self.record(
            OperationName::Read,
            CapabilitySet::file(file),
            OperationOutcome::Observed,
        );
        Ok(buffer)
    }

    pub fn write(&mut self, file: &FileCap, lease: &ExclusiveLease, data: &[u8]) -> Result<usize> {
        self.require_scope(file.scope(), lease.scope())?;
        self.revalidate_file(file)?;
        self.revalidate_lock(lease)?;
        // SAFETY: data pointer and length remain valid for the syscall.
        let count =
            unsafe { libc::write(file.raw_fd(), data.as_ptr().cast::<c_void>(), data.len()) };
        if count < 0 {
            return Err(io_error("write"));
        }
        self.record(
            OperationName::Write,
            CapabilitySet::write(file, lease),
            OperationOutcome::Applied,
        );
        Ok(count as usize)
    }

    pub fn fsync(&mut self, file: &FileCap) -> Result<()> {
        self.revalidate_file(file)?;
        // SAFETY: fd is retained by FileCap.
        let result = unsafe { libc::fsync(file.raw_fd()) };
        if result < 0 {
            return Err(io_error("fsync"));
        }
        self.record(
            OperationName::Fsync,
            CapabilitySet::file(file),
            OperationOutcome::Observed,
        );
        Ok(())
    }

    fn revalidate_file(&mut self, file: &FileCap) -> Result<()> {
        let observed = FileIdentity::from_fd(file.raw_fd())?;
        if observed.inode_key() != file.identity().inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        Ok(())
    }

    pub fn pidfd_open(&mut self, pid: libc::pid_t) -> Result<PidFdCap> {
        // SAFETY: pid is a value and flags are valid.
        let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) as c_int };
        if fd < 0 {
            return Err(io_error("pidfd_open"));
        }
        // SAFETY: fd is freshly returned and uniquely owned.
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        let identity = FileIdentity::from_fd(fd.as_raw_fd())?;
        let cap = PidFdCap::new(fd, identity, self.next_id(), fresh_scope());
        self.record(
            OperationName::PidfdOpen,
            CapabilitySet::pidfd(&cap),
            OperationOutcome::Observed,
        );
        Ok(cap)
    }

    pub fn pidfd_signal(&mut self, pidfd: &PidFdCap, signal: c_int) -> Result<()> {
        self.revalidate_pidfd(pidfd)?;
        // SAFETY: pidfd is retained by PidFdCap; pidfd_send_signal accepts a
        // null siginfo pointer for the ordinary signal operation.
        let result = unsafe {
            libc::syscall(libc::SYS_pidfd_send_signal, pidfd.raw_fd(), signal, 0, 0) as c_int
        };
        if result < 0 {
            return Err(io_error("pidfd_send_signal"));
        }
        self.record(
            OperationName::PidfdSignal,
            CapabilitySet::pidfd(pidfd),
            OperationOutcome::Applied,
        );
        Ok(())
    }

    fn revalidate_pidfd(&mut self, pidfd: &PidFdCap) -> Result<()> {
        let observed = FileIdentity::from_fd(pidfd.raw_fd())?;
        if observed.inode_key() != pidfd.identity().inode_key() {
            return Err(BoundaryError::IdentityMismatch);
        }
        Ok(())
    }

    pub fn flock_exclusive(&mut self, lock: LockCap, deadline_ns: u64) -> Result<ExclusiveLease> {
        let identity = lock.identity();
        let id = lock.id();
        let operation = libc::LOCK_EX | libc::LOCK_NB;
        loop {
            if self.clock.now_ns() >= deadline_ns {
                return Err(BoundaryError::BudgetExceeded("flock"));
            }
            // SAFETY: fd is retained by the owned LockCap and operation is a
            // value.  On success ownership moves into ExclusiveLease.
            if unsafe { libc::flock(lock.raw_fd(), operation) } == 0 {
                self.record(
                    OperationName::Flock,
                    CapabilitySet::lock(&lock),
                    OperationOutcome::Applied,
                );
                let scope = lock.scope();
                let fd = lock.fd;
                return Ok(ExclusiveLease::new(fd, identity, id, scope));
            }
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::EWOULDBLOCK)
                && error.raw_os_error() != Some(libc::EAGAIN)
            {
                return Err(BoundaryError::Io {
                    operation: "flock",
                    source: error,
                });
            }
            let now = self.clock.now_ns();
            if now >= deadline_ns {
                return Err(BoundaryError::BudgetExceeded("flock"));
            }
            self.clock
                .sleep_ns(1_000_000.min(deadline_ns.saturating_sub(now)));
        }
    }

    pub fn into_parts(self) -> BoundaryParts<O> {
        BoundaryParts {
            observer: self.observer,
            quarantine_obligations: QuarantineObligations {
                obligations: self.pending_quarantines,
            },
            stage_obligations: StageObligations {
                obligations: self.pending_stages,
            },
        }
    }

    pub fn finish(self) -> std::result::Result<O, BoundaryFinishError<O>> {
        if self.pending_quarantines.is_empty() && self.pending_stages.is_empty() {
            Ok(self.observer)
        } else {
            Err(BoundaryFinishError {
                observer: self.observer,
                quarantine_obligations: QuarantineObligations {
                    obligations: self.pending_quarantines,
                },
                stage_obligations: StageObligations {
                    obligations: self.pending_stages,
                },
            })
        }
    }

    pub fn into_observer(self) -> std::result::Result<O, BoundaryFinishError<O>> {
        self.finish()
    }

    /// Explicitly consume a capability when a transaction closes it. Drop is
    /// still the safety net, while the record makes close part of the
    /// deterministic operation trace rather than an invisible side effect.
    pub fn close_dir(&mut self, capability: DirCap) {
        self.record(
            OperationName::Close,
            CapabilitySet::close_directory(&capability),
            OperationOutcome::Applied,
        );
        drop(capability);
    }

    pub fn close_file(&mut self, capability: FileCap) {
        self.record(
            OperationName::Close,
            CapabilitySet::close_file(&capability),
            OperationOutcome::Applied,
        );
        drop(capability);
    }

    pub fn close_pidfd(&mut self, capability: PidFdCap) {
        self.record(
            OperationName::Close,
            CapabilitySet::close_pidfd(&capability),
            OperationOutcome::Applied,
        );
        drop(capability);
    }

    pub fn close_lock(&mut self, capability: LockCap) {
        self.record(
            OperationName::Close,
            CapabilitySet::close_lock(&capability),
            OperationOutcome::Applied,
        );
        drop(capability);
    }

    pub fn close_exclusive_lease(&mut self, capability: ExclusiveLease) {
        self.record(
            OperationName::Close,
            CapabilitySet::close_lease(&capability),
            OperationOutcome::Applied,
        );
        drop(capability);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;
    use std::fs;
    use std::os::unix::fs::symlink;
    use std::path::PathBuf;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(label: &str) -> Self {
            let mut path = std::env::temp_dir();
            path.push(format!("darling-lifecycle-{label}-{}", std::process::id()));
            let _ = fs::remove_dir_all(&path);
            fs::create_dir(&path).expect("create test root");
            Self(path)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.0);
        }
    }

    fn terminal_ids_match_allocations(allocated: &[u64], terminals: &[u64]) -> bool {
        let allocated_set: HashSet<_> = allocated.iter().copied().collect();
        let terminal_set: HashSet<_> = terminals.iter().copied().collect();
        allocated.len() == allocated_set.len()
            && terminals.len() == terminal_set.len()
            && allocated_set == terminal_set
    }

    struct SwapAfterBind {
        root: PathBuf,
        fired: bool,
    }

    impl FaultInjector for SwapAfterBind {
        fn invalidates_quiescence(&self) -> bool {
            self.fired
        }

        fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
            if checkpoint == Checkpoint::AfterBindBeforePublish && !self.fired {
                self.fired = true;
                fs::create_dir(self.root.join("created")).expect("replacement directory");
                return Err(BoundaryError::Injected(checkpoint));
            }
            Ok(())
        }
    }

    struct SwapBeforeMutation {
        root: PathBuf,
        fired: bool,
    }

    impl FaultInjector for SwapBeforeMutation {
        fn invalidates_quiescence(&self) -> bool {
            self.fired
        }

        fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
            if checkpoint == Checkpoint::BeforeExactMutation && !self.fired {
                self.fired = true;
                fs::rename(self.root.join("target"), self.root.join("old"))
                    .expect("move target inode");
                fs::write(self.root.join("target"), b"replacement").expect("replacement file");
                return Ok(());
            }
            Ok(())
        }
    }

    struct ReplacementAfterMove {
        root: PathBuf,
        fired: bool,
    }

    impl FaultInjector for ReplacementAfterMove {
        fn invalidates_quiescence(&self) -> bool {
            self.fired
        }

        fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
            if checkpoint == Checkpoint::AfterMoveBeforeVerify && !self.fired {
                self.fired = true;
                fs::write(self.root.join("target"), b"replacement").expect("replacement file");
                return Err(BoundaryError::Injected(checkpoint));
            }
            Ok(())
        }
    }

    struct ReplacementAfterQuarantine {
        root: PathBuf,
        stage_name: PathBuf,
        fired: bool,
    }

    struct ReplacementAfterQuarantineVerify {
        root: PathBuf,
        quarantine_name: PathBuf,
        fired: bool,
    }

    impl FaultInjector for ReplacementAfterQuarantineVerify {
        fn invalidates_quiescence(&self) -> bool {
            self.fired
        }

        fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
            if checkpoint == Checkpoint::AfterQuarantineVerifyBeforeGc && !self.fired {
                self.fired = true;
                let old_name = self.root.join(".quarantine-original");
                fs::rename(self.root.join(&self.quarantine_name), &old_name)
                    .expect("move verified quarantine inode");
                fs::create_dir(self.root.join(&self.quarantine_name))
                    .expect("replacement quarantine directory");
                return Ok(());
            }
            Ok(())
        }
    }

    impl FaultInjector for ReplacementAfterQuarantine {
        fn invalidates_quiescence(&self) -> bool {
            self.fired
        }

        fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
            if checkpoint == Checkpoint::AfterQuarantineMoveBeforeVerify && !self.fired {
                self.fired = true;
                fs::create_dir(self.root.join(&self.stage_name))
                    .expect("replacement staging directory");
                return Err(BoundaryError::Injected(checkpoint));
            }
            Ok(())
        }
    }

    fn make_lock(root: &Path) {
        fs::write(root.join("lock"), b"").expect("lock file");
        let mut permissions = fs::metadata(root.join("lock"))
            .expect("lock metadata")
            .permissions();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            permissions.set_mode(0o600);
        }
        fs::set_permissions(root.join("lock"), permissions).expect("lock mode");
    }

    fn path_cstring(path: &Path) -> CString {
        CString::new(path.as_os_str().as_bytes()).expect("test path has no NUL")
    }

    fn install_external_scope<C: Clock, O: Observer, F: FaultInjector>(
        boundary: &mut Boundary<C, O, F>,
        parent: &DirCap,
        lease: &ExclusiveLease,
    ) {
        // SAFETY: the single-threaded unit fixture has no namespace writers
        // after this point; it models the external lifecycle barrier required
        // before quarantine GC.
        let scope = unsafe { QuiescentScope::from_external(parent, lease) }
            .expect("external quiescent scope");
        boundary.set_quiescent_scope(scope);
    }

    fn install_external_scope_pair<C: Clock, O: Observer, F: FaultInjector>(
        boundary: &mut Boundary<C, O, F>,
        source: &DirCap,
        destination: &DirCap,
        lease: &ExclusiveLease,
    ) {
        // SAFETY: the sibling-directory fixture has no namespace writers
        // after this handoff; it models one barrier covering both parents.
        let scope = unsafe { QuiescentScope::from_external_pair(source, destination, lease) }
            .expect("external paired quiescent scope");
        boundary.set_quiescent_scope(scope);
    }

    #[test]
    fn symlink_anchor_is_rejected() {
        let temp = TempDir::new("symlink");
        fs::create_dir(temp.0.join("real")).expect("real");
        symlink(temp.0.join("real"), temp.0.join("alias")).expect("alias");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        assert!(boundary.anchor_directory(&temp.0.join("alias")).is_err());
    }

    #[test]
    fn readdir_error_is_not_accepted_as_eof() {
        readdir_end_or_error(0).expect("clean EOF");
        assert!(matches!(
            readdir_end_or_error(libc::EIO),
            Err(BoundaryError::Io {
                operation: "readdir",
                source,
            }) if source.raw_os_error() == Some(libc::EIO)
        ));
    }

    #[test]
    fn mkdir_rollback_never_deletes_replacement() {
        let temp = TempDir::new("mkdir-swap");
        make_lock(&temp.0);
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            SwapAfterBind {
                root: temp.0.clone(),
                fired: false,
            },
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        let result = boundary.mkdir_child(&parent, &lease, "created", 0o700);
        assert!(result.is_err());
        assert!(temp.0.join("created").is_dir(), "replacement was deleted");
    }

    #[test]
    fn lease_scope_cannot_authorize_another_root() {
        let temp = TempDir::new("scope-brand");
        fs::create_dir(temp.0.join("a")).expect("root a");
        fs::create_dir(temp.0.join("b")).expect("root b");
        make_lock(&temp.0.join("a"));
        make_lock(&temp.0.join("b"));
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent_a = boundary
            .anchor_directory(&temp.0.join("a"))
            .expect("anchor a");
        let parent_b = boundary
            .anchor_directory(&temp.0.join("b"))
            .expect("anchor b");
        let lock_b = boundary.open_lock(&parent_b, "lock").expect("lock b");
        let lease_b = boundary
            .flock_exclusive(lock_b, 10_000_000)
            .expect("lease b");
        assert!(matches!(
            boundary.mkdir_child(&parent_a, &lease_b, "foreign", 0o700),
            Err(BoundaryError::WrongCapability("authority scope"))
        ));
        assert!(!temp.0.join("a/foreign").exists());
    }

    #[test]
    fn mkdir_bind_fault_never_name_deletes() {
        let temp = TempDir::new("mkdir-bind-fault");
        make_lock(&temp.0);
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            FailAt::new(Checkpoint::AfterMkdirBeforeBind),
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        assert!(boundary
            .mkdir_child(&parent, &lease, "created", 0o700)
            .is_err());
        assert!(
            !temp.0.join("created").exists(),
            "unbound failure published the final name"
        );
        assert!(
            !fs::read_dir(&temp.0)
                .expect("stage listing")
                .flatten()
                .any(|entry| entry
                    .file_name()
                    .to_string_lossy()
                    .starts_with(".lifecycle-stage-")),
            "registered staging transaction leaked"
        );
        let parts = boundary.into_parts();
        assert_eq!(parts.quarantine_obligations.len(), 2);
        assert_eq!(parts.stage_obligations.len(), 0);
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::StageRegistered && record.identity().is_observed()
        }));
        let registered = records
            .iter()
            .position(|record| {
                record.operation() == OperationName::StageRegistered
                    && record.identity().is_observed()
            })
            .expect("stage registration");
        let checkpoint = records
            .iter()
            .position(|record| {
                record.operation() == OperationName::Checkpoint
                    && record.checkpoint() == Some(Checkpoint::AfterStageMkdirBeforeBind)
            })
            .expect("stage checkpoint");
        assert!(
            registered < checkpoint,
            "checkpoint preceded stage registration"
        );
    }

    #[test]
    fn mkdir_publishes_only_after_child_capability_is_bound() {
        let temp = TempDir::new("mkdir-publish");
        make_lock(&temp.0);
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope(&mut boundary, &parent, &lease);
        let child = boundary
            .mkdir_child(&parent, &lease, "created", 0o700)
            .expect("mkdir");
        assert_eq!(
            child.identity().inode_key(),
            FileIdentity::from_at(parent.raw_fd(), &CString::new("created").expect("name"))
                .expect("published identity")
                .inode_key()
        );
        drop(child);
        let records = boundary
            .into_observer()
            .ok()
            .expect("no quarantine obligations")
            .records;
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::MkdirChild
                && record.outcome() == OperationOutcome::Applied
                && record.mutation().changed()
        }));
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::StageCleanup
                && record.outcome() == OperationOutcome::Applied
        }));
        assert!(records.iter().any(|record| {
            matches!(
                record.journal(),
                JournalRecord::Stage(StageJournal::Cleanup {
                    terminal: StageCleanupTerminal::Cleaned,
                    ..
                })
            )
        }));
        assert!(records.iter().any(|record| {
            matches!(
                record.journal(),
                JournalRecord::Quarantine(QuarantineEvent::Collected { .. })
            )
        }));
    }

    #[test]
    fn unlink_exact_rejects_replacement_after_checkpoint() {
        let temp = TempDir::new("unlink-swap");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"original").expect("target");
        let mut stat = MaybeUninit::<libc_stat>::zeroed();
        // SAFETY: stat initializes the buffer on success.
        assert_eq!(
            unsafe {
                libc::stat(
                    path_cstring(&temp.0.join("target")).as_ptr(),
                    stat.as_mut_ptr(),
                )
            },
            0
        );
        // SAFETY: stat returned success.
        let expected = FileIdentity::from_stat(unsafe { &stat.assume_init() });
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            SwapBeforeMutation {
                root: temp.0.clone(),
                fired: false,
            },
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope(&mut boundary, &parent, &lease);
        let result = boundary.unlink_exact(&parent, &lease, "target", expected, false);
        assert!(matches!(result, Err(BoundaryError::QuarantineRequired)));
        assert_eq!(
            fs::read(temp.0.join("target")).expect("replacement"),
            b"replacement"
        );
    }

    #[test]
    fn rollback_failure_is_recorded_as_partial() {
        let temp = TempDir::new("rollback-journal");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"original").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            ReplacementAfterMove {
                root: temp.0.clone(),
                fired: false,
            },
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        assert!(boundary
            .unlink_exact(&parent, &lease, "target", expected, false)
            .is_err());
        let parts = boundary.into_parts();
        assert_eq!(parts.quarantine_obligations.len(), 0);
        let stage_id = parts
            .observer
            .records
            .iter()
            .find_map(|record| match record.journal() {
                JournalRecord::Stage(StageJournal::Allocated { stage, .. }) => Some(stage.id()),
                _ => None,
            })
            .expect("allocated rollback stage");
        assert_eq!(parts.stage_obligations.len(), 1);
        let stage_obligations = parts.stage_obligations.into_inner();
        match &stage_obligations[0] {
            StageObligation::Bound(obligation) => {
                assert_eq!(obligation.id(), stage_id);
                assert_eq!(
                    obligation.stage_identity().mode & libc::S_IFMT,
                    libc::S_IFDIR
                );
            }
            StageObligation::Unbound(_) => panic!("rollback retained a bound stage"),
        }
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::UnlinkExact
                && record.outcome() == OperationOutcome::RollbackIncomplete
                && record.mutation().changed()
        }));
        assert_eq!(
            fs::read(temp.0.join("target")).expect("replacement"),
            b"replacement"
        );
    }

    #[test]
    fn restored_after_quarantine_move_retains_stage_authority() {
        let temp = TempDir::new("restored-quarantine-stage");
        make_lock(&temp.0);
        fs::create_dir(temp.0.join("stage")).expect("stage");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            FailAt::new(Checkpoint::AfterQuarantineMoveBeforeVerify),
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        let stage = boundary
            .open_directory(&parent, "stage")
            .expect("stage cap");
        let stage_id = stage.id();
        let stage_name = CString::new("stage").expect("stage name");
        assert!(matches!(
            boundary.cleanup_registered(&parent, &lease, &stage_name, &stage, true),
            Err(BoundaryError::Injected(
                Checkpoint::AfterQuarantineMoveBeforeVerify
            ))
        ));
        assert!(
            temp.0.join("stage").is_dir(),
            "restore lost the stage inode"
        );
        let error = match boundary.finish() {
            Err(error) => error,
            Ok(_) => panic!("restored stage authority was silently dropped"),
        };
        assert_eq!(error.quarantine_obligations.len(), 0);
        assert_eq!(error.stage_obligations.len(), 1);
        let stage_obligations = error.stage_obligations.into_inner();
        assert!(matches!(
            &stage_obligations[0],
            StageObligation::Bound(obligation) if obligation.id() == stage_id
        ));
    }

    #[test]
    fn pre_quarantine_cleanup_failure_has_only_stage_obligation() {
        let temp = TempDir::new("pre-quarantine-cleanup");
        fs::create_dir(temp.0.join("a")).expect("root a");
        fs::create_dir(temp.0.join("b")).expect("root b");
        make_lock(&temp.0.join("a"));
        make_lock(&temp.0.join("b"));
        fs::create_dir(temp.0.join("a/stage")).expect("stage");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent_a = boundary
            .anchor_directory(&temp.0.join("a"))
            .expect("anchor a");
        let parent_b = boundary
            .anchor_directory(&temp.0.join("b"))
            .expect("anchor b");
        let lock_b = boundary.open_lock(&parent_b, "lock").expect("lock b");
        let lease_b = boundary
            .flock_exclusive(lock_b, 10_000_000)
            .expect("lease b");
        let stage = boundary
            .open_directory(&parent_a, "stage")
            .expect("stage cap");
        let stage_name = CString::new("stage").expect("stage name");
        assert!(matches!(
            boundary.cleanup_registered(&parent_a, &lease_b, &stage_name, &stage, true),
            Err(BoundaryError::WrongCapability("authority scope"))
        ));
        let parts = boundary.into_parts();
        assert_eq!(parts.stage_obligations.len(), 1);
        assert_eq!(parts.quarantine_obligations.len(), 0);
        let stage_obligations = parts.stage_obligations.into_inner();
        assert!(matches!(
            &stage_obligations[0],
            StageObligation::Bound(obligation) if obligation.id() == stage.id()
        ));
    }

    #[test]
    fn quarantine_without_external_scope_remains_recovery_obligation() {
        let temp = TempDir::new("quarantine-obligation");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"payload").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            FailAt::new(Checkpoint::AfterMoveBeforeVerify),
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        assert!(matches!(
            boundary.unlink_exact(&parent, &lease, "target", expected, false),
            Err(BoundaryError::QuarantineRequired)
        ));
        let obligations = boundary.take_quarantine_obligations();
        assert_eq!(obligations.len(), 1);
        assert_eq!(obligations.into_inner().len(), 1);
        assert_eq!(boundary.take_stage_obligations().len(), 0);
        assert!(fs::read_dir(&temp.0)
            .expect("root listing")
            .flatten()
            .any(|entry| entry
                .file_name()
                .to_string_lossy()
                .starts_with(".lifecycle-quarantine-")));
    }

    #[test]
    fn exact_mutation_requires_external_quiescence_scope() {
        let temp = TempDir::new("exact-mutation-scope");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"payload").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        assert!(matches!(
            boundary.unlink_exact(&parent, &lease, "target", expected, false),
            Err(BoundaryError::QuarantineRequired)
        ));
        let parts = boundary.into_parts();
        assert_eq!(parts.stage_obligations.len(), 0);
        assert_eq!(parts.quarantine_obligations.len(), 1);
        assert_eq!(
            fs::read(temp.0.join("target")).expect("restored target"),
            b"payload"
        );
    }

    #[test]
    fn rename_exact_requires_external_quiescence_scope() {
        let temp = TempDir::new("rename-mutation-scope");
        make_lock(&temp.0);
        fs::write(temp.0.join("source"), b"payload").expect("source");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("source"))
                .expect("source fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        assert!(matches!(
            boundary.rename_exact(&parent, &parent, &lease, "source", "destination", expected,),
            Err(BoundaryError::QuarantineRequired)
        ));
        assert!(temp.0.join("source").exists());
        assert!(!temp.0.join("destination").exists());
    }

    #[test]
    fn rename_exact_accepts_external_scope_for_sibling_directories() {
        let temp = TempDir::new("rename-sibling-scope");
        fs::create_dir(temp.0.join("source-parent")).expect("source parent");
        fs::create_dir(temp.0.join("destination-parent")).expect("destination parent");
        make_lock(&temp.0.join("source-parent"));
        fs::write(temp.0.join("source-parent/source"), b"payload").expect("source");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("source-parent/source"))
                .expect("source fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let source_parent = boundary
            .anchor_directory(&temp.0.join("source-parent"))
            .expect("source anchor");
        let destination_parent = boundary
            .anchor_directory(&temp.0.join("destination-parent"))
            .expect("destination anchor");
        let lock = boundary.open_lock(&source_parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope_pair(&mut boundary, &source_parent, &destination_parent, &lease);
        boundary
            .rename_exact(
                &source_parent,
                &destination_parent,
                &lease,
                "source",
                "destination",
                expected,
            )
            .expect("sibling rename");
        assert!(!temp.0.join("source-parent/source").exists());
        assert_eq!(
            fs::read(temp.0.join("destination-parent/destination")).expect("destination"),
            b"payload"
        );
    }

    #[test]
    fn finish_refuses_outstanding_quarantine_obligations() {
        let temp = TempDir::new("quarantine-finish");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"payload").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            FailAt::new(Checkpoint::AfterMoveBeforeVerify),
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        assert!(matches!(
            boundary.unlink_exact(&parent, &lease, "target", expected, false),
            Err(BoundaryError::QuarantineRequired)
        ));
        let error = match boundary.finish() {
            Err(error) => error,
            Ok(_) => panic!("finish accepted a pending quarantine"),
        };
        assert_eq!(error.quarantine_obligations.len(), 1);
        assert!(!error.quarantine_obligations.is_empty());
        assert_eq!(error.stage_obligations.len(), 0);
    }

    #[test]
    fn quarantine_restore_failure_after_move_is_tracked_and_replacement_survives() {
        let temp = TempDir::new("quarantine-swap");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"original").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            ReplacementAfterQuarantine {
                root: temp.0.clone(),
                stage_name: PathBuf::from(".lifecycle-stage-3"),
                fired: false,
            },
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope(&mut boundary, &parent, &lease);
        assert!(boundary
            .unlink_exact(&parent, &lease, "target", expected, false)
            .is_err());
        assert!(temp.0.join(".lifecycle-stage-3").is_dir());
        assert!(fs::read_dir(&temp.0)
            .expect("root listing")
            .flatten()
            .any(|entry| entry
                .file_name()
                .to_string_lossy()
                .starts_with(".lifecycle-quarantine-")));
        let parts = boundary.into_parts();
        assert_eq!(parts.quarantine_obligations.len(), 1);
        assert_eq!(parts.stage_obligations.len(), 0);
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::StageCleanup
                && record.outcome() == OperationOutcome::Error
        }));
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::UnlinkExact
                && record.outcome() == OperationOutcome::MutatedError
                && record.mutation().changed()
        }));
        assert!(records.iter().any(|record| {
            matches!(
                record.journal(),
                JournalRecord::Quarantine(QuarantineEvent::BoundRecovery { .. })
            )
        }));
    }

    #[test]
    fn quarantine_gc_rechecks_after_verify_before_unlink() {
        let temp = TempDir::new("quarantine-gc-swap");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"original").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            ReplacementAfterQuarantineVerify {
                root: temp.0.clone(),
                quarantine_name: PathBuf::from(".lifecycle-quarantine-4"),
                fired: false,
            },
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope(&mut boundary, &parent, &lease);
        assert!(boundary
            .unlink_exact(&parent, &lease, "target", expected, false)
            .is_err());
        assert!(temp.0.join(".lifecycle-quarantine-4").is_dir());
        assert!(temp.0.join(".quarantine-original").is_dir());
        let parts = boundary.into_parts();
        assert_eq!(parts.quarantine_obligations.len(), 1);
        assert_eq!(parts.stage_obligations.len(), 0);
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::StageCleanup
                && record.outcome() == OperationOutcome::Error
        }));
    }

    #[test]
    fn expired_flock_deadline_does_not_attempt_acquisition() {
        let temp = TempDir::new("flock-deadline");
        make_lock(&temp.0);
        let mut boundary = Boundary::new(ScriptedClock::new(0), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        assert!(matches!(
            boundary.flock_exclusive(lock, 0),
            Err(BoundaryError::BudgetExceeded("flock"))
        ));
        assert!(!boundary
            .into_observer()
            .ok()
            .expect("no quarantine obligations")
            .records
            .iter()
            .any(|record| record.operation() == OperationName::Flock));
    }

    #[test]
    fn post_mutation_fault_records_partial_unlink() {
        let temp = TempDir::new("post-mutation");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"payload").expect("target");
        let expected = FileIdentity::from_fd(
            fs::File::open(temp.0.join("target"))
                .expect("target fd")
                .as_raw_fd(),
        )
        .expect("identity");
        let mut boundary = Boundary::new(
            RealClock::default(),
            VecObserver::default(),
            FailAt::new(Checkpoint::AfterExactMutation),
        );
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope(&mut boundary, &parent, &lease);
        assert!(boundary
            .unlink_exact(&parent, &lease, "target", expected, false)
            .is_err());
        assert!(!temp.0.join("target").exists());
        let records = boundary
            .into_observer()
            .ok()
            .expect("no quarantine obligations")
            .records;
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::UnlinkExact
                && record.outcome() == OperationOutcome::MutatedError
                && record.mutation().changed()
        }));
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::Checkpoint
                && record.checkpoint() == Some(Checkpoint::AfterExactMutation)
                && record.outcome() == OperationOutcome::Injected
        }));
    }

    #[test]
    fn observer_and_bounds_cover_operations() {
        let temp = TempDir::new("observer");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"payload").expect("target");
        fs::write(temp.0.join("other"), b"other").expect("other");
        let mut boundary = Boundary::new(ScriptedClock::new(0), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("flock");
        install_external_scope(&mut boundary, &parent, &lease);
        let file = boundary.open_file(&parent, "target", true).expect("file");
        assert_eq!(boundary.read(&file, 64).expect("read"), b"payload");
        boundary.write(&file, &lease, b"!").expect("write");
        boundary.fsync(&file).expect("fsync");
        let first = boundary.list_names(&parent, 8, 10_000).expect("list");
        let second = boundary
            .list_names(&parent, 8, 10_000)
            .expect("second list");
        assert_eq!(first.len(), 3);
        assert_eq!(first, second, "listing must use an independent OFD cursor");
        boundary.now_ns();
        boundary
            .rename_exact(
                &parent,
                &parent,
                &lease,
                "other",
                "renamed",
                FileIdentity::from_fd(
                    fs::File::open(temp.0.join("other"))
                        .expect("other fd")
                        .as_raw_fd(),
                )
                .expect("other identity"),
            )
            .expect("rename");
        boundary.close_dir(parent);
        let records = boundary
            .into_observer()
            .ok()
            .expect("no quarantine obligations")
            .records;
        assert!(records
            .iter()
            .any(|record| record.operation() == OperationName::RenameExact));
        assert!(records.iter().any(|record| {
            record.operation() == OperationName::StageCleanup
                && record.outcome() == OperationOutcome::Applied
        }));
        assert!(records
            .iter()
            .any(|record| record.operation() == OperationName::Close));
    }

    #[test]
    fn journal_capability_roles_are_typed_per_operation() {
        let temp = TempDir::new("typed-capability-roles");
        make_lock(&temp.0);
        fs::write(temp.0.join("target"), b"payload").expect("target");
        fs::write(temp.0.join("other"), b"other").expect("other");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let lock = boundary.open_lock(&parent, "lock").expect("lock");
        let lease = boundary.flock_exclusive(lock, 10_000_000).expect("lease");
        install_external_scope(&mut boundary, &parent, &lease);
        let file = boundary.open_file(&parent, "target", true).expect("file");
        boundary.write(&file, &lease, b"!").expect("write");
        let child = boundary
            .mkdir_child(&parent, &lease, "created", 0o700)
            .expect("mkdir");
        let published_identity = child.identity();
        let other_identity = FileIdentity::from_fd(
            fs::File::open(temp.0.join("other"))
                .expect("other fd")
                .as_raw_fd(),
        )
        .expect("other identity");
        boundary
            .rename_exact(&parent, &parent, &lease, "other", "renamed", other_identity)
            .expect("rename");
        drop(child);
        let records = boundary
            .into_observer()
            .ok()
            .expect("no obligations")
            .records;

        let write_roles = records.iter().find_map(|record| match record.journal() {
            JournalRecord::Operation(OperationEvent {
                operation: OperationName::Write,
                capabilities,
                ..
            }) => Some(
                capabilities
                    .as_slice()
                    .iter()
                    .map(|entry| entry.role())
                    .collect::<Vec<_>>(),
            ),
            _ => None,
        });
        assert_eq!(
            write_roles,
            Some(vec![CapabilityRole::File, CapabilityRole::Lease])
        );
        let mkdir_roles = records.iter().find_map(|record| match record.journal() {
            JournalRecord::Operation(OperationEvent {
                operation: OperationName::MkdirChild,
                result: OperationOutcome::Applied,
                capabilities,
                ..
            }) => Some(capabilities.as_slice()),
            _ => None,
        });
        assert_eq!(
            mkdir_roles.map(|entries| entries.iter().map(|entry| entry.role()).collect::<Vec<_>>()),
            Some(vec![
                CapabilityRole::Parent,
                CapabilityRole::Lease,
                CapabilityRole::Stage,
                CapabilityRole::Child,
            ])
        );
        let rename_roles = records.iter().find_map(|record| match record.journal() {
            JournalRecord::Operation(OperationEvent {
                operation: OperationName::RenameExact,
                result: OperationOutcome::Applied,
                capabilities,
                ..
            }) => Some(capabilities.as_slice()),
            _ => None,
        });
        assert_eq!(
            rename_roles
                .map(|entries| entries.iter().map(|entry| entry.role()).collect::<Vec<_>>()),
            Some(vec![
                CapabilityRole::SourceParent,
                CapabilityRole::DestinationParent,
                CapabilityRole::Lease,
            ])
        );

        let mut stage_allocations = Vec::new();
        let mut stage_registrations = Vec::new();
        let mut stage_terminals = Vec::new();
        let mut stage_cleanups = Vec::new();
        let mut published = Vec::new();
        let mut quarantine_terminals = Vec::new();
        for record in records {
            match record.journal() {
                JournalRecord::Stage(StageJournal::Allocated {
                    stage,
                    capabilities,
                    ..
                }) => {
                    let parent = capabilities
                        .as_slice()
                        .iter()
                        .find(|entry| entry.role() == CapabilityRole::Parent)
                        .expect("allocated stage has parent");
                    stage_allocations.push((stage.id(), parent.id()));
                }
                JournalRecord::Stage(StageJournal::Registered {
                    stage,
                    capabilities,
                    ..
                }) => {
                    let parent = capabilities
                        .as_slice()
                        .iter()
                        .find(|entry| entry.role() == CapabilityRole::Parent)
                        .expect("registered stage has parent");
                    stage_registrations.push((stage.id(), parent.id()));
                }
                JournalRecord::Stage(StageJournal::Published {
                    stage,
                    capabilities,
                    identity: IdentityObservation::Observed(identity),
                    ..
                }) => {
                    assert_eq!(
                        identity.inode_key(),
                        published_identity.inode_key(),
                        "published identity must match the bound child"
                    );
                    let source_parent = capabilities
                        .as_slice()
                        .iter()
                        .find(|entry| entry.role() == CapabilityRole::SourceParent)
                        .expect("published stage has source parent");
                    let destination_parent = capabilities
                        .as_slice()
                        .iter()
                        .find(|entry| entry.role() == CapabilityRole::DestinationParent)
                        .expect("published stage has destination parent");
                    published.push((stage.id(), source_parent.id(), destination_parent.id()));
                    stage_terminals.push(stage.id());
                }
                JournalRecord::Stage(StageJournal::Cleanup { stage, .. }) => {
                    assert!(
                        !stage_terminals.contains(&stage.id()),
                        "stage cleanup terminal emitted twice"
                    );
                    stage_terminals.push(stage.id());
                    stage_cleanups.push(stage.id());
                }
                JournalRecord::Quarantine(
                    QuarantineEvent::Restored { object, .. }
                    | QuarantineEvent::BoundRecovery { object, .. }
                    | QuarantineEvent::UnboundRecovery { object, .. }
                    | QuarantineEvent::Collected { object, .. },
                ) => {
                    assert!(
                        !quarantine_terminals.contains(&object.id()),
                        "quarantine terminal emitted twice"
                    );
                    quarantine_terminals.push(object.id());
                }
                _ => {}
            }
        }
        assert_eq!(
            stage_allocations.len(),
            3,
            "mkdir and rename stage allocations"
        );
        assert_eq!(
            stage_registrations.len(),
            3,
            "mkdir and rename stage registrations"
        );
        assert!(terminal_ids_match_allocations(
            &stage_allocations
                .iter()
                .map(|(stage_id, _)| *stage_id)
                .collect::<Vec<_>>(),
            &stage_terminals
        ));
        assert_eq!(stage_cleanups.len(), quarantine_terminals.len());
        let child_stage = stage_allocations
            .iter()
            .find(|(_, parent_id)| *parent_id != parent.id())
            .expect("child stage topology");
        assert_eq!(
            stage_registrations
                .iter()
                .find(|(stage_id, _)| *stage_id == child_stage.0)
                .map(|(_, parent_id)| *parent_id),
            Some(child_stage.1)
        );
        assert_eq!(published, vec![(child_stage.0, child_stage.1, parent.id())]);
    }

    #[test]
    fn stage_terminality_rejects_missing_foreign_and_duplicate_terminals() {
        assert!(terminal_ids_match_allocations(&[1, 2], &[1, 2]));
        assert!(!terminal_ids_match_allocations(&[1, 2], &[1]));
        assert!(!terminal_ids_match_allocations(&[1, 2], &[1, 3]));
        assert!(!terminal_ids_match_allocations(&[1, 2], &[1, 2, 2]));
    }

    #[test]
    fn finish_refuses_unbound_stage_obligation() {
        let temp = TempDir::new("stage-obligation-finish");
        make_lock(&temp.0);
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let parent = boundary.anchor_directory(&temp.0).expect("anchor");
        let stage = StageRef::new(77);
        boundary.register_unbound_stage(
            &parent,
            stage,
            Some(ChildRef::new(78)),
            CString::new("entry").expect("name"),
            None,
            None,
        );
        let error = match boundary.finish() {
            Err(error) => error,
            Ok(_) => panic!("finish accepted a pending stage obligation"),
        };
        assert!(error.quarantine_obligations.is_empty());
        assert_eq!(error.stage_obligations.len(), 1);
        let obligations = error.stage_obligations.into_inner();
        assert!(!obligations[0].is_bound());
        match &obligations[0] {
            StageObligation::Unbound(obligation) => {
                assert_eq!(obligation.id(), stage.id());
                assert!(obligation.has_retained_parent());
            }
            StageObligation::Bound(_) => panic!("expected unbound stage obligation"),
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn pidfd_signal_is_capability_bound() {
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        let pidfd = boundary
            .pidfd_open(std::process::id() as libc::pid_t)
            .expect("pidfd");
        boundary.pidfd_signal(&pidfd, 0).expect("signal zero");
    }

    #[allow(dead_code)]
    fn _compile_time_caps_are_not_clone() {
        fn assert_send<T: Send>() {}
        assert_send::<DirCap>();
        assert_send::<FileCap>();
        assert_send::<PidFdCap>();
        assert_send::<LockCap>();
        assert_send::<ExclusiveLease>();
    }
}
