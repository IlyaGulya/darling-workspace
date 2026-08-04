//! Authoritative lifecycle operation boundary for dar-4ush.2.
//!
//! The public API is intentionally capability-shaped.  Paths are accepted
//! only by [`Boundary::anchor_directory`].  All later operations use retained
//! directory/file/pidfd/lock capabilities, and the capability newtypes own
//! their `OwnedFd` with RAII.  No capability implements `Clone`.

use libc::{self, c_int, c_void, stat as libc_stat};
use std::ffi::{CStr, CString, OsStr};
use std::fmt;
use std::io;
use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

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

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
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

    fn from_fd(fd: RawFd) -> Result<Self> {
        let mut value = MaybeUninit::<libc_stat>::zeroed();
        // SAFETY: fstat initializes the supplied stat buffer on success.
        let result = unsafe { libc::fstat(fd, value.as_mut_ptr()) };
        if result < 0 {
            return Err(io_error("fstat"));
        }
        // SAFETY: fstat returned success, so the buffer is initialized.
        Ok(Self::from_stat(unsafe { &value.assume_init() }))
    }

    fn from_at(parent: RawFd, name: &CString) -> Result<Self> {
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
/// anchored parent for the duration of one quarantine GC transaction.
///
/// The operation boundary deliberately has no constructor that discovers or
/// asserts quiescence.  A lifecycle controller/explorer must establish the
/// writer-stop barrier and then create this value through the explicit unsafe
/// handoff.  Without this scope, [`QuarantinedObject`] remains a recovery
/// obligation and cannot be unlinked by the boundary.
pub struct QuiescentScope {
    parent_identity: FileIdentity,
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
            lease_identity,
            scope: parent.scope(),
            token: NEXT_SCOPE_ID.fetch_add(1, Ordering::Relaxed),
        })
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

#[must_use = "boundary parts contain recovery obligations"]
pub struct BoundaryParts<O> {
    pub observer: O,
    pub quarantine_obligations: QuarantineObligations,
}

#[must_use = "boundary finish failed with recovery obligations"]
pub struct BoundaryFinishError<O> {
    pub observer: O,
    pub quarantine_obligations: QuarantineObligations,
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
    StageCleanup,
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

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct OperationRecord {
    pub sequence: u64,
    pub operation: OperationName,
    pub capability_ids: Vec<u64>,
    pub result: &'static str,
    pub checkpoint: Option<Checkpoint>,
    pub mutated: bool,
    pub identity: Option<FileIdentity>,
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
    let mut names = Vec::new();
    loop {
        if clock.now_ns() >= deadline_ns {
            // SAFETY: directory is a live DIR* owned by this function.
            unsafe { libc::closedir(directory) };
            return Err(BoundaryError::BudgetExceeded("directory deadline"));
        }
        // SAFETY: readdir returns a borrowed entry valid until the next call.
        let entry = unsafe { libc::readdir(directory) };
        if entry.is_null() {
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
            // SAFETY: directory is a live DIR* owned by this function.
            unsafe { libc::closedir(directory) };
            return Err(BoundaryError::BudgetExceeded("directory item limit"));
        }
        names.push(name);
    }
    // SAFETY: directory is a live DIR* owned by this function.
    if unsafe { libc::closedir(directory) } < 0 {
        return Err(io_error("closedir"));
    }
    Ok(names)
}

pub struct Boundary<C: Clock, O: Observer, F: FaultInjector> {
    clock: C,
    observer: O,
    faults: F,
    sequence: u64,
    next_id: u64,
    quiescent_scope: Option<QuiescentScope>,
    pending_quarantines: Vec<QuarantineObligation>,
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
            pending_quarantines: Vec::new(),
        }
    }

    /// Install a writer-stop proof supplied by the lifecycle controller.  A
    /// boundary never derives this scope from its own directory or lock
    /// checks; callers must explicitly hand it over after quiescing writers.
    pub fn set_quiescent_scope(&mut self, scope: QuiescentScope) {
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

    fn next_id(&mut self) -> u64 {
        self.next_id += 1;
        self.next_id
    }

    fn record_with(
        &mut self,
        operation: OperationName,
        ids: Vec<u64>,
        result: &'static str,
        checkpoint: Option<Checkpoint>,
        mutated: bool,
    ) {
        self.record_with_identity(operation, ids, result, checkpoint, mutated, None);
    }

    fn record_with_identity(
        &mut self,
        operation: OperationName,
        ids: Vec<u64>,
        result: &'static str,
        checkpoint: Option<Checkpoint>,
        mutated: bool,
        identity: Option<FileIdentity>,
    ) {
        self.sequence += 1;
        self.observer.record(OperationRecord {
            sequence: self.sequence,
            operation,
            capability_ids: ids,
            result,
            checkpoint,
            mutated,
            identity,
        });
    }

    fn record(&mut self, operation: OperationName, ids: Vec<u64>, result: &'static str) {
        self.record_with(operation, ids, result, None, false);
    }

    fn fail_or_record<T>(
        &mut self,
        operation: OperationName,
        ids: Vec<u64>,
        result: Result<T>,
    ) -> Result<T> {
        self.record(operation, ids, if result.is_ok() { "ok" } else { "error" });
        result
    }

    fn checkpoint(&mut self, checkpoint: Checkpoint) -> Result<()> {
        let result = self.faults.checkpoint(checkpoint);
        self.record_with(
            OperationName::Checkpoint,
            Vec::new(),
            if result.is_ok() { "ok" } else { "injected" },
            Some(checkpoint),
            false,
        );
        result
    }

    pub fn now_ns(&mut self) -> u64 {
        let value = self.clock.now_ns();
        self.record(OperationName::ClockNow, Vec::new(), "ok");
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
        self.record(OperationName::AnchorDirectory, vec![cap.id()], "ok");
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
            vec![capability.id()],
            result,
        )
    }

    pub fn open_file(&mut self, parent: &DirCap, name: &str, writable: bool) -> Result<FileCap> {
        self.revalidate_directory(parent, false)?;
        let c_name = c_name(name)?;
        let fd = open_file_at(parent.raw_fd(), &c_name, writable)?;
        let identity = FileIdentity::from_fd(fd.as_raw_fd())?;
        let cap = FileCap::new(fd, identity, self.next_id(), parent.scope());
        self.record(OperationName::OpenChild, vec![parent.id(), cap.id()], "ok");
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
        self.record(OperationName::OpenLock, vec![parent.id(), cap.id()], "ok");
        Ok(cap)
    }

    pub fn open_directory(&mut self, parent: &DirCap, name: &str) -> Result<DirCap> {
        self.revalidate_directory(parent, false)?;
        let c_name = c_name(name)?;
        let fd = open_dir_at(parent.raw_fd(), &c_name)?;
        let identity = FileIdentity::from_fd(fd.as_raw_fd())?;
        let cap = DirCap::new(fd, identity, self.next_id(), parent.scope());
        self.record(OperationName::OpenChild, vec![parent.id(), cap.id()], "ok");
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
        // The final public name is not touched until the child has an
        // identity-bound fd.  This closes the mkdirat->bind window with a
        // transaction-owned staging directory.
        let (stage_name, stage) = match self.create_staging_dir(parent, lease) {
            Ok(value) => value,
            Err(error) => {
                self.record_with(
                    OperationName::MkdirChild,
                    vec![parent.id(), lease.id()],
                    "staged-error",
                    None,
                    true,
                );
                return Err(error);
            }
        };
        let staged_name = CString::new("entry").expect("literal has no NUL");
        if let Err(error) = mkdir_at(stage.raw_fd(), &staged_name, mode) {
            let cleanup = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                vec![parent.id(), lease.id(), stage.id()],
                if cleanup.is_ok() {
                    "staged-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
            );
            return Err(error);
        }
        let child_id = self.next_id();
        self.record_with_identity(
            OperationName::StageRegistered,
            vec![parent.id(), lease.id(), stage.id(), child_id],
            "allocated",
            None,
            true,
            None,
        );
        let child_identity = match FileIdentity::from_at(stage.raw_fd(), &staged_name) {
            Ok(identity) => identity,
            Err(error) => {
                let cleanup_child = open_dir_at(stage.raw_fd(), &staged_name)
                    .and_then(|fd| {
                        FileIdentity::from_fd(fd.as_raw_fd()).map(|identity| (fd, identity))
                    })
                    .and_then(|(fd, identity)| {
                        let child = DirCap::new(fd, identity, child_id, stage.scope());
                        self.cleanup_registered(&stage, lease, &staged_name, &child, true)
                    });
                let cleanup_stage =
                    self.cleanup_registered(parent, lease, &stage_name, &stage, true);
                self.record_with(
                    OperationName::MkdirChild,
                    vec![parent.id(), lease.id(), stage.id()],
                    if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                        "staged-error"
                    } else {
                        "rollback-incomplete"
                    },
                    None,
                    true,
                );
                return Err(error);
            }
        };
        self.record_with_identity(
            OperationName::StageRegistered,
            vec![parent.id(), lease.id(), stage.id(), child_id],
            "registered",
            None,
            true,
            Some(child_identity),
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
                vec![parent.id(), lease.id(), stage.id(), child_id],
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                    vec![parent.id(), lease.id(), stage.id(), child_id],
                    if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                        "staged-error"
                    } else {
                        "rollback-incomplete"
                    },
                    None,
                    true,
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
                    vec![parent.id(), lease.id(), stage.id(), child_id],
                    if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                        "staged-error"
                    } else {
                        "rollback-incomplete"
                    },
                    None,
                    true,
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
                vec![parent.id(), lease.id(), stage.id(), child_id],
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    "identity-mismatch"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
            );
            return Err(BoundaryError::IdentityMismatch);
        }
        let cap = DirCap::new(fd, identity, child_id, stage.scope());
        if let Err(error) = self.checkpoint(Checkpoint::AfterBindBeforePublish) {
            let cleanup_child = self.cleanup_registered(&stage, lease, &staged_name, &cap, true);
            let cleanup_stage = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                vec![parent.id(), lease.id(), cap.id()],
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
            );
            return Err(error);
        }
        if let Err(error) = rename_at(stage.raw_fd(), &staged_name, parent.raw_fd(), &final_name) {
            let cleanup_child = self.cleanup_registered(&stage, lease, &staged_name, &cap, true);
            let cleanup_stage = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::MkdirChild,
                vec![parent.id(), lease.id(), cap.id()],
                if cleanup_child.is_ok() && cleanup_stage.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
            );
            return Err(error);
        }
        if let Err(error) = self.cleanup_registered(parent, lease, &stage_name, &stage, true) {
            self.record_with(
                OperationName::MkdirChild,
                vec![parent.id(), lease.id(), cap.id()],
                "mutated-error",
                None,
                true,
            );
            return Err(error);
        }
        self.record_with(
            OperationName::MkdirChild,
            vec![parent.id(), lease.id(), cap.id()],
            "ok",
            None,
            true,
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
        self.require_scope(parent.scope(), lease.scope())?;
        self.require_scope(parent.scope(), scope.scope)?;
        self.require_scope(parent.scope(), object.scope)?;
        let observed_parent = self.revalidate_directory(parent, false)?;
        if observed_parent.inode_key() != scope.parent_identity.inode_key()
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
            self.pending_quarantines
                .push(QuarantineObligation::Bound(object));
            return Err(BoundaryError::QuarantineRequired);
        };
        let result = self.gc_quarantine(parent, lease, &scope, &object);
        self.quiescent_scope = Some(scope);
        if result.is_err() {
            self.pending_quarantines
                .push(QuarantineObligation::Bound(object));
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
        let result = self
            .rollback_created(parent, lease, name, cap, directory)
            .and_then(|object| self.finish_quarantine(parent, lease, object));
        self.record_with_identity(
            OperationName::StageCleanup,
            vec![parent.id(), lease.id(), cap.id()],
            if result.is_ok() { "ok" } else { "error" },
            None,
            true,
            Some(cap.identity()),
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
        self.require_scope(parent.scope(), lease.scope())?;
        let result = self
            .rollback_named(parent, lease, name, expected, true)
            .and_then(|object| self.finish_quarantine(parent, lease, object));
        self.record_with_identity(
            OperationName::StageCleanup,
            vec![parent.id(), lease.id(), capability_id],
            if result.is_ok() { "ok" } else { "error" },
            None,
            true,
            Some(expected),
        );
        result
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
            return error;
        }
        self.pending_quarantines.push(match bound {
            Some(object) => QuarantineObligation::Bound(object),
            None => QuarantineObligation::Unbound(unbound),
        });
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
        let object = QuarantinedObject {
            fd,
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
                self.pending_quarantines
                    .push(QuarantineObligation::Bound(object));
                return Err(BoundaryError::IdentityMismatch);
            }
            Err(BoundaryError::Io { source, .. })
                if source.raw_os_error() == Some(libc::ENOENT) => {}
            Err(error) => {
                self.pending_quarantines
                    .push(QuarantineObligation::Bound(object));
                return Err(error);
            }
        }
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
        self.record_with_identity(
            OperationName::StageRegistered,
            vec![parent.id(), lease.id(), stage_id],
            "allocated",
            None,
            true,
            None,
        );
        let identity = match FileIdentity::from_at(parent.raw_fd(), &stage_name) {
            Ok(identity) => identity,
            Err(error) => {
                self.record_with_identity(
                    OperationName::StageCleanup,
                    vec![parent.id(), lease.id(), stage_id],
                    "deferred",
                    None,
                    true,
                    None,
                );
                return Err(error);
            }
        };
        self.record_with_identity(
            OperationName::StageRegistered,
            vec![parent.id(), lease.id(), stage_id],
            "registered",
            None,
            true,
            Some(identity),
        );
        // Before the child is bound, fail closed.  There is deliberately no
        // name-only rollback because a replacement could now occupy the name.
        if let Err(error) = self.checkpoint(Checkpoint::AfterStageMkdirBeforeBind) {
            let cleanup = self
                .rollback_named(parent, lease, &stage_name, identity, true)
                .and_then(|object| self.finish_quarantine(parent, lease, object));
            self.record_with_identity(
                OperationName::StageCleanup,
                vec![parent.id(), lease.id(), stage_id],
                if cleanup.is_ok() { "ok" } else { "error" },
                None,
                true,
                Some(identity),
            );
            return Err(error);
        }
        let fd = match open_dir_at(parent.raw_fd(), &stage_name) {
            Ok(fd) => fd,
            Err(error) => {
                let cleanup = self
                    .rollback_named(parent, lease, &stage_name, identity, true)
                    .and_then(|object| self.finish_quarantine(parent, lease, object));
                self.record_with_identity(
                    OperationName::StageCleanup,
                    vec![parent.id(), lease.id(), stage_id],
                    if cleanup.is_ok() { "ok" } else { "error" },
                    None,
                    true,
                    Some(identity),
                );
                return Err(error);
            }
        };
        let bound_identity = match FileIdentity::from_fd(fd.as_raw_fd()) {
            Ok(identity) => identity,
            Err(error) => {
                let cleanup = self
                    .rollback_named(parent, lease, &stage_name, identity, true)
                    .and_then(|object| self.finish_quarantine(parent, lease, object));
                self.record_with_identity(
                    OperationName::StageCleanup,
                    vec![parent.id(), lease.id(), stage_id],
                    if cleanup.is_ok() { "ok" } else { "error" },
                    None,
                    true,
                    Some(identity),
                );
                return Err(error);
            }
        };
        if bound_identity.inode_key() != identity.inode_key() {
            let cleanup = self
                .rollback_named(parent, lease, &stage_name, identity, true)
                .and_then(|object| self.finish_quarantine(parent, lease, object));
            self.record_with_identity(
                OperationName::StageCleanup,
                vec![parent.id(), lease.id(), stage_id],
                if cleanup.is_ok() { "ok" } else { "error" },
                None,
                true,
                Some(identity),
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
        self.restore_staged(stage, parent, staged_name, original_name)?;
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
        let ids = vec![parent.id(), lease.id()];
        if let Err(error) = self.exact_identity(parent, lease, &name, expected) {
            self.record_with(OperationName::UnlinkExact, ids, "error", None, false);
            return Err(error);
        }
        let staged_name = CString::new("entry").expect("literal has no NUL");
        let (stage_name, stage) = match self.create_staging_dir(parent, lease) {
            Ok(value) => value,
            Err(error) => {
                self.record_with(OperationName::UnlinkExact, ids, "staged-error", None, true);
                return Err(error);
            }
        };
        let moved = rename_at(parent.raw_fd(), &name, stage.raw_fd(), &staged_name);
        if let Err(error) = moved {
            let cleanup = self.cleanup_registered(parent, lease, &stage_name, &stage, true);
            self.record_with(
                OperationName::UnlinkExact,
                ids,
                if cleanup.is_ok() {
                    "staged-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                ids,
                if rollback.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                    ids,
                    if rollback.is_ok() {
                        "rolled-back-error"
                    } else {
                        "rollback-incomplete"
                    },
                    None,
                    true,
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
                ids,
                if rollback.is_ok() {
                    "identity-mismatch"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                ids,
                if rollback.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        if let Err(error) = self.cleanup_registered(parent, lease, &stage_name, &stage, true) {
            self.record_with(OperationName::UnlinkExact, ids, "mutated-error", None, true);
            return Err(error);
        }
        if let Err(error) = self.checkpoint(Checkpoint::AfterExactMutation) {
            self.record_with(OperationName::UnlinkExact, ids, "mutated-error", None, true);
            return Err(error);
        }
        self.record_with(OperationName::UnlinkExact, ids, "ok", None, true);
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
        let ids = vec![source_parent.id(), destination_parent.id(), lease.id()];
        if let Err(error) = self.require_scope(source_parent.scope(), lease.scope()) {
            self.record_with(OperationName::RenameExact, ids, "error", None, false);
            return Err(error);
        }
        if let Err(error) = self.require_scope(source_parent.scope(), destination_parent.scope()) {
            self.record_with(OperationName::RenameExact, ids, "error", None, false);
            return Err(error);
        }
        if let Err(error) = self.exact_identity(source_parent, lease, &source, expected) {
            self.record_with(OperationName::RenameExact, ids, "error", None, false);
            return Err(error);
        }
        if let Err(error) = self.revalidate_directory(destination_parent, false) {
            self.record_with(OperationName::RenameExact, ids, "error", None, false);
            return Err(error);
        }
        let staged_name = CString::new("entry").expect("literal has no NUL");
        let (stage_name, stage) = match self.create_staging_dir(source_parent, lease) {
            Ok(value) => value,
            Err(error) => {
                self.record_with(OperationName::RenameExact, ids, "staged-error", None, true);
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
                ids,
                if cleanup.is_ok() {
                    "staged-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                ids,
                if rollback.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                    ids,
                    if rollback.is_ok() {
                        "rolled-back-error"
                    } else {
                        "rollback-incomplete"
                    },
                    None,
                    true,
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
                ids,
                if rollback.is_ok() {
                    "identity-mismatch"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
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
                ids,
                if rollback.is_ok() {
                    "rolled-back-error"
                } else {
                    "rollback-incomplete"
                },
                None,
                true,
            );
            return Err(rollback.err().unwrap_or(error));
        }
        if let Err(error) = self.cleanup_registered(source_parent, lease, &stage_name, &stage, true)
        {
            self.record_with(OperationName::RenameExact, ids, "mutated-error", None, true);
            return Err(error);
        }
        if let Err(error) = self.checkpoint(Checkpoint::AfterExactMutation) {
            self.record_with(OperationName::RenameExact, ids, "mutated-error", None, true);
            return Err(error);
        }
        self.record_with(OperationName::RenameExact, ids, "ok", None, true);
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
        self.fail_or_record(OperationName::ListNames, vec![parent.id()], result)
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
        self.record(OperationName::Read, vec![file.id()], "ok");
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
        self.record(OperationName::Write, vec![file.id(), lease.id()], "ok");
        Ok(count as usize)
    }

    pub fn fsync(&mut self, file: &FileCap) -> Result<()> {
        self.revalidate_file(file)?;
        // SAFETY: fd is retained by FileCap.
        let result = unsafe { libc::fsync(file.raw_fd()) };
        if result < 0 {
            return Err(io_error("fsync"));
        }
        self.record(OperationName::Fsync, vec![file.id()], "ok");
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
        self.record(OperationName::PidfdOpen, vec![cap.id()], "ok");
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
        self.record(OperationName::PidfdSignal, vec![pidfd.id()], "ok");
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
                self.record(OperationName::Flock, vec![lock.id()], "ok");
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
        }
    }

    pub fn finish(self) -> std::result::Result<O, BoundaryFinishError<O>> {
        if self.pending_quarantines.is_empty() {
            Ok(self.observer)
        } else {
            Err(BoundaryFinishError {
                observer: self.observer,
                quarantine_obligations: QuarantineObligations {
                    obligations: self.pending_quarantines,
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
        self.record(OperationName::Close, vec![capability.id()], "ok");
        drop(capability);
    }

    pub fn close_file(&mut self, capability: FileCap) {
        self.record(OperationName::Close, vec![capability.id()], "ok");
        drop(capability);
    }

    pub fn close_pidfd(&mut self, capability: PidFdCap) {
        self.record(OperationName::Close, vec![capability.id()], "ok");
        drop(capability);
    }

    pub fn close_lock(&mut self, capability: LockCap) {
        self.record(OperationName::Close, vec![capability.id()], "ok");
        drop(capability);
    }

    pub fn close_exclusive_lease(&mut self, capability: ExclusiveLease) {
        self.record(OperationName::Close, vec![capability.id()], "ok");
        drop(capability);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
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

    struct SwapAfterBind {
        root: PathBuf,
        fired: bool,
    }

    impl FaultInjector for SwapAfterBind {
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

    #[test]
    fn symlink_anchor_is_rejected() {
        let temp = TempDir::new("symlink");
        fs::create_dir(temp.0.join("real")).expect("real");
        symlink(temp.0.join("real"), temp.0.join("alias")).expect("alias");
        let mut boundary = Boundary::new(RealClock::default(), VecObserver::default(), NoFault);
        assert!(boundary.anchor_directory(&temp.0.join("alias")).is_err());
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
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation == OperationName::StageRegistered && record.identity.is_some()
        }));
        let registered = records
            .iter()
            .position(|record| {
                record.operation == OperationName::StageRegistered && record.identity.is_some()
            })
            .expect("stage registration");
        let checkpoint = records
            .iter()
            .position(|record| {
                record.operation == OperationName::Checkpoint
                    && record.checkpoint == Some(Checkpoint::AfterStageMkdirBeforeBind)
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
            record.operation == OperationName::MkdirChild && record.result == "ok" && record.mutated
        }));
        assert!(
            records
                .iter()
                .any(|record| record.operation == OperationName::StageCleanup
                    && record.result == "ok")
        );
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
        assert!(matches!(result, Err(BoundaryError::IdentityMismatch)));
        assert_eq!(
            fs::read(temp.0.join("target")).expect("replacement"),
            b"replacement"
        );
    }

    #[test]
    fn rollback_failure_is_recorded_as_mutated() {
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
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation == OperationName::UnlinkExact
                && record.result == "rollback-incomplete"
                && record.mutated
        }));
        assert_eq!(
            fs::read(temp.0.join("target")).expect("replacement"),
            b"replacement"
        );
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
        assert!(fs::read_dir(&temp.0)
            .expect("root listing")
            .flatten()
            .any(|entry| entry
                .file_name()
                .to_string_lossy()
                .starts_with(".lifecycle-quarantine-")));
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
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation == OperationName::StageCleanup && record.result == "error"
        }));
        assert!(records.iter().any(|record| {
            record.operation == OperationName::UnlinkExact
                && record.result == "mutated-error"
                && record.mutated
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
        let records = parts.observer.records;
        assert!(records.iter().any(|record| {
            record.operation == OperationName::StageCleanup && record.result == "error"
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
            .any(|record| record.operation == OperationName::Flock));
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
            record.operation == OperationName::UnlinkExact
                && record.result == "mutated-error"
                && record.mutated
        }));
        assert!(records.iter().any(|record| {
            record.operation == OperationName::Checkpoint
                && record.checkpoint == Some(Checkpoint::AfterExactMutation)
                && record.result == "injected"
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
            .any(|record| record.operation == OperationName::RenameExact));
        assert!(
            records
                .iter()
                .any(|record| record.operation == OperationName::StageCleanup
                    && record.result == "ok")
        );
        assert!(records
            .iter()
            .any(|record| record.operation == OperationName::Close));
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
