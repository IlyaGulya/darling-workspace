//! Rust-owned E-UNION transaction pilot.
//!
//! Darlingserver may transport these bounded requests, but it cannot construct
//! retained namespace authority or perform a mutation.  V1 intentionally
//! supports only unambiguous upper-layer operations.  Lower-only, both-layer,
//! whiteout and absent-source operations fail closed before mutation.

use libc::{self, c_int};
use std::collections::HashMap;
use std::ffi::{CStr, CString};
use std::fmt;
use std::io;
use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};

const MAX_PATH_BYTES: usize = 1024;
const MAX_COMPONENTS: usize = 64;
const MAX_REPLAY: usize = 128;
const MAX_RECOVERY: usize = MAX_REPLAY * 2;
const MAX_JOURNAL_BYTES: usize = 1024 * 1024;
const JOURNAL_NAME: &CStr = c"transactions.wal";
const QUARANTINE_NAME: &CStr = c"quarantine";

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct TransactionId(pub [u8; 16]);

impl TransactionId {
    pub fn new(bytes: [u8; 16]) -> Result<Self, TransactionError> {
        if bytes == [0; 16] {
            return Err(TransactionError::Protocol("zero transaction id"));
        }
        Ok(Self(bytes))
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Request {
    Create {
        id: TransactionId,
        path: Vec<u8>,
        flags: i32,
        mode: u32,
    },
    Mkdir {
        id: TransactionId,
        path: Vec<u8>,
        mode: u32,
    },
    Unlink {
        id: TransactionId,
        path: Vec<u8>,
        flags: i32,
    },
    Rename {
        id: TransactionId,
        source: Vec<u8>,
        destination: Vec<u8>,
    },
}

impl Request {
    fn id(&self) -> TransactionId {
        match self {
            Self::Create { id, .. }
            | Self::Mkdir { id, .. }
            | Self::Unlink { id, .. }
            | Self::Rename { id, .. } => *id,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum LayerState {
    Absent,
    UpperOnly,
    LowerOnly,
    Both,
    Whiteout,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum Outcome {
    Created { device: u64, inode: u64 },
    Mutated,
    Rejected { errno: i32, reason: &'static str },
    RecoveryRequired { operation: &'static str },
}

#[derive(Debug)]
pub enum RecoveryObligation {
    BoundCreated {
        path: Vec<u8>,
        device: u64,
        inode: u64,
        object: OwnedFd,
    },
    UnboundCreated {
        path: Vec<u8>,
        device: u64,
        inode: u64,
    },
    BoundQuarantine {
        operation: &'static str,
        parent: OwnedFd,
        staged_name: Vec<u8>,
        device: u64,
        inode: u64,
        object: OwnedFd,
    },
    DisplacedSource {
        operation: &'static str,
        original_path: Vec<u8>,
        device: u64,
        inode: u64,
        object: OwnedFd,
    },
}

#[derive(Debug)]
struct GarbageObligation {
    id: TransactionId,
    obligation: RecoveryObligation,
}

#[derive(Debug)]
pub enum TransactionError {
    Io(&'static str, io::Error),
    Protocol(&'static str),
    Revoked,
    IdentityMismatch,
}

impl fmt::Display for TransactionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(op, err) => write!(f, "{op}: {err}"),
            Self::Protocol(message) => write!(f, "transaction protocol: {message}"),
            Self::Revoked => write!(f, "transaction service revoked"),
            Self::IdentityMismatch => write!(f, "retained namespace identity mismatch"),
        }
    }
}

impl std::error::Error for TransactionError {}

fn io(op: &'static str) -> TransactionError {
    TransactionError::Io(op, io::Error::last_os_error())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Identity {
    device: u64,
    inode: u64,
}

fn identity(fd: RawFd) -> Result<Identity, TransactionError> {
    let mut stat = MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, stat.as_mut_ptr()) } != 0 {
        return Err(io("fstat retained root"));
    }
    let stat = unsafe { stat.assume_init() };
    Ok(Identity {
        device: stat.st_dev,
        inode: stat.st_ino,
    })
}

fn duplicate(fd: RawFd) -> Result<OwnedFd, TransactionError> {
    let duplicated = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 3) };
    if duplicated < 0 {
        return Err(io("duplicate retained root"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(duplicated) })
}

fn component(bytes: &[u8]) -> Result<CString, TransactionError> {
    if bytes.is_empty()
        || bytes == b"."
        || bytes == b".."
        || bytes.contains(&0)
        || bytes.contains(&b'/')
    {
        return Err(TransactionError::Protocol("invalid path component"));
    }
    CString::new(bytes).map_err(|_| TransactionError::Protocol("NUL path component"))
}

fn components(path: &[u8]) -> Result<Vec<CString>, TransactionError> {
    if path.is_empty() || path.len() > MAX_PATH_BYTES || path[0] == b'/' {
        return Err(TransactionError::Protocol("unbounded or absolute path"));
    }
    let result: Vec<_> = path
        .split(|byte| *byte == b'/')
        .map(component)
        .collect::<Result<_, _>>()?;
    if result.is_empty() || result.len() > MAX_COMPONENTS {
        return Err(TransactionError::Protocol("path component budget"));
    }
    Ok(result)
}

fn open_dir(parent: RawFd, name: &CStr) -> Result<OwnedFd, TransactionError> {
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io("open retained parent"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

#[repr(C)]
struct OpenHow {
    flags: u64,
    mode: u64,
    resolve: u64,
}

fn open_logical_dir(root: RawFd, parts: &[CString]) -> Result<OwnedFd, TransactionError> {
    let mut path = Vec::new();
    for (index, part) in parts.iter().enumerate() {
        if index != 0 {
            path.push(b'/');
        }
        path.extend_from_slice(part.as_bytes());
    }
    let path = CString::new(path).map_err(|_| TransactionError::Protocol("logical directory"))?;
    let how = OpenHow {
        flags: (libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC) as u64,
        mode: 0,
        // RESOLVE_NO_MAGICLINKS | RESOLVE_IN_ROOT. Relative and absolute
        // symlinks are resolved as guest paths beneath the retained layer.
        resolve: 0x02 | 0x10,
    };
    let fd = unsafe {
        libc::syscall(
            libc::SYS_openat2,
            root,
            path.as_ptr(),
            &how,
            std::mem::size_of::<OpenHow>(),
        ) as libc::c_int
    };
    if fd < 0 {
        return Err(io("open logical retained parent"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn exists(parent: RawFd, name: &CStr) -> Result<bool, TransactionError> {
    let mut stat = MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            stat.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } == 0
    {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ENOENT) {
        Ok(false)
    } else {
        Err(TransactionError::Io("fstatat layer", error))
    }
}

struct ResolvedParent {
    upper: OwnedFd,
    lower: Option<OwnedFd>,
    leaf: CString,
}

pub struct GuestNamespaceTransactionService {
    upper: OwnedFd,
    lower: OwnedFd,
    _sidecar: OwnedFd,
    quarantine: OwnedFd,
    journal: OwnedFd,
    upper_identity: Identity,
    lower_identity: Identity,
    generation: u64,
    active: bool,
    replay: HashMap<TransactionId, (Vec<u8>, Outcome)>,
    created: HashMap<TransactionId, OwnedFd>,
    recovery: Vec<RecoveryObligation>,
    pending_created: Option<OwnedFd>,
    pending_commit: HashMap<TransactionId, RecoveryObligation>,
    garbage: Vec<GarbageObligation>,
    #[cfg(test)]
    hook: Option<Box<dyn FnMut(TestCheckpoint) + Send>>,
    #[cfg(test)]
    journal_fault: Option<JournalFault>,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TestCheckpoint {
    AfterResolve,
    AfterMutation,
    AfterLeafAuthority,
    AfterQuarantineMove,
    AfterQuarantineVerify,
    AfterPublication,
    AfterCommit,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum JournalFault {
    CommitWrite,
    CommitFsync,
}

impl GuestNamespaceTransactionService {
    pub fn from_retained_roots(
        upper: RawFd,
        lower: RawFd,
        sidecar: RawFd,
        generation: u64,
    ) -> Result<Self, TransactionError> {
        if generation == 0 {
            return Err(TransactionError::Protocol("zero generation"));
        }
        let upper = duplicate(upper)?;
        let lower = duplicate(lower)?;
        let sidecar = duplicate(sidecar)?;
        let quarantine = Self::open_or_create_directory(sidecar.as_raw_fd(), QUARANTINE_NAME)?;
        let mut journal_fd = unsafe {
            libc::openat(
                sidecar.as_raw_fd(),
                JOURNAL_NAME.as_ptr(),
                libc::O_RDWR
                    | libc::O_CREAT
                    | libc::O_EXCL
                    | libc::O_APPEND
                    | libc::O_CLOEXEC
                    | libc::O_NOFOLLOW,
                0o600,
            )
        };
        let journal_created = journal_fd >= 0;
        if journal_fd < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EEXIST) {
            journal_fd = unsafe {
                libc::openat(
                    sidecar.as_raw_fd(),
                    JOURNAL_NAME.as_ptr(),
                    libc::O_RDWR | libc::O_APPEND | libc::O_CLOEXEC | libc::O_NOFOLLOW,
                    0,
                )
            };
        }
        if journal_fd < 0 {
            return Err(io("open durable transaction journal"));
        }
        let journal = unsafe { OwnedFd::from_raw_fd(journal_fd) };
        if journal_created && unsafe { libc::fchmod(journal.as_raw_fd(), 0o600) } != 0 {
            let _ = unsafe { libc::unlinkat(sidecar.as_raw_fd(), JOURNAL_NAME.as_ptr(), 0) };
            return Err(io("normalize durable transaction journal mode"));
        }
        let mut journal_stat = MaybeUninit::<libc::stat>::uninit();
        if unsafe { libc::fstat(journal.as_raw_fd(), journal_stat.as_mut_ptr()) } != 0 {
            return Err(io("fstat durable transaction journal"));
        }
        let journal_stat = unsafe { journal_stat.assume_init() };
        if journal_stat.st_mode & libc::S_IFMT != libc::S_IFREG
            || journal_stat.st_mode & 0o777 != 0o600
            || journal_stat.st_uid != unsafe { libc::geteuid() }
            || journal_stat.st_nlink != 1
        {
            return Err(TransactionError::IdentityMismatch);
        }
        Self::fsync_directory(sidecar.as_raw_fd())?;
        let mut service = Self {
            upper_identity: identity(upper.as_raw_fd())?,
            lower_identity: identity(lower.as_raw_fd())?,
            upper,
            lower,
            _sidecar: sidecar,
            quarantine,
            journal,
            generation,
            active: true,
            replay: HashMap::new(),
            created: HashMap::new(),
            recovery: Vec::new(),
            pending_created: None,
            pending_commit: HashMap::new(),
            garbage: Vec::new(),
            #[cfg(test)]
            hook: None,
            #[cfg(test)]
            journal_fault: None,
        };
        service.recover_journal()?;
        Ok(service)
    }

    fn open_or_create_directory(parent: RawFd, name: &CStr) -> Result<OwnedFd, TransactionError> {
        let created = if unsafe { libc::mkdirat(parent, name.as_ptr(), 0o700) } == 0 {
            true
        } else if io::Error::last_os_error().raw_os_error() == Some(libc::EEXIST) {
            false
        } else {
            return Err(io("mkdir private quarantine"));
        };
        if created && unsafe { libc::fchmodat(parent, name.as_ptr(), 0o700, 0) } != 0 {
            let _ = unsafe { libc::unlinkat(parent, name.as_ptr(), libc::AT_REMOVEDIR) };
            return Err(io("normalize private quarantine mode"));
        }
        let fd = unsafe {
            libc::openat(
                parent,
                name.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            return Err(io("open private quarantine"));
        }
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        let value = identity(fd.as_raw_fd())?;
        let mut stat = MaybeUninit::<libc::stat>::uninit();
        if unsafe { libc::fstat(fd.as_raw_fd(), stat.as_mut_ptr()) } != 0 {
            return Err(io("fstat private quarantine"));
        }
        let stat = unsafe { stat.assume_init() };
        if stat.st_mode & libc::S_IFMT != libc::S_IFDIR
            || stat.st_mode & 0o077 != 0
            || stat.st_uid != unsafe { libc::geteuid() }
            || value.inode == 0
        {
            return Err(TransactionError::IdentityMismatch);
        }
        Ok(fd)
    }

    fn fsync_directory(fd: RawFd) -> Result<(), TransactionError> {
        let path = CString::new(format!("/proc/self/fd/{fd}"))
            .map_err(|_| TransactionError::Protocol("directory fd path"))?;
        let reopened = unsafe {
            libc::open(
                path.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        if reopened < 0 {
            return Err(io("reopen directory for fsync"));
        }
        let reopened = unsafe { OwnedFd::from_raw_fd(reopened) };
        if unsafe { libc::fsync(reopened.as_raw_fd()) } != 0 {
            return Err(io("fsync transaction directory"));
        }
        Ok(())
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn revoke(&mut self) {
        self.active = false;
        self.collect_garbage();
    }

    #[cfg(test)]
    pub fn set_test_hook(&mut self, hook: impl FnMut(TestCheckpoint) + Send + 'static) {
        self.hook = Some(Box::new(hook));
    }

    #[cfg(test)]
    pub fn set_journal_fault(&mut self, fault: JournalFault) {
        self.journal_fault = Some(fault);
    }

    #[cfg(test)]
    fn checkpoint(&mut self, checkpoint: TestCheckpoint) {
        if let Some(hook) = self.hook.as_mut() {
            hook(checkpoint);
        }
    }

    pub fn duplicate_created_result(
        &self,
        id: TransactionId,
        expected_device: u64,
        expected_inode: u64,
    ) -> Result<OwnedFd, TransactionError> {
        if !self.active {
            return Err(TransactionError::Revoked);
        }
        let retained = self
            .created
            .get(&id)
            .ok_or(TransactionError::Protocol("created FD unavailable"))?;
        let fd = duplicate(retained.as_raw_fd())?;
        let actual = identity(fd.as_raw_fd())?;
        if actual.device != expected_device || actual.inode != expected_inode {
            return Err(TransactionError::IdentityMismatch);
        }
        Ok(fd)
    }

    pub fn recovery_obligations(&self) -> &[RecoveryObligation] {
        &self.recovery
    }

    pub fn has_outstanding_recovery(&self) -> bool {
        !self.recovery.is_empty() || !self.pending_commit.is_empty()
    }

    pub fn prepare_clean_sidecar(&mut self) -> Result<(), TransactionError> {
        self.active = false;
        self.collect_garbage();
        if !self.recovery.is_empty() || !self.pending_commit.is_empty() || !self.garbage.is_empty()
        {
            return Err(TransactionError::Protocol(
                "recovery prevents sidecar cleanup",
            ));
        }
        if unsafe { libc::ftruncate(self.journal.as_raw_fd(), 0) } != 0
            || unsafe { libc::fsync(self.journal.as_raw_fd()) } != 0
        {
            return Err(io("truncate clean transaction journal"));
        }
        if unsafe { libc::unlinkat(self.sidecar_fd(), JOURNAL_NAME.as_ptr(), 0) } != 0 {
            return Err(io("unlink clean transaction journal"));
        }
        if unsafe {
            libc::unlinkat(
                self.sidecar_fd(),
                QUARANTINE_NAME.as_ptr(),
                libc::AT_REMOVEDIR,
            )
        } != 0
        {
            return Err(io("remove clean private quarantine"));
        }
        Self::fsync_directory(self.sidecar_fd())
    }

    #[cfg(test)]
    pub(crate) fn discard_forensic_sidecar_for_contract(&mut self) -> Result<(), TransactionError> {
        self.recovery.clear();
        self.pending_commit.clear();
        self.garbage.clear();
        self.prepare_clean_sidecar()
    }

    fn sidecar_fd(&self) -> RawFd {
        self._sidecar.as_raw_fd()
    }

    pub fn execute(&mut self, request: Request) -> Result<Outcome, TransactionError> {
        if !self.active {
            return Err(TransactionError::Revoked);
        }
        match &request {
            Request::Create { path, mode, .. } | Request::Mkdir { path, mode, .. } => {
                if mode & !0o7777 != 0 {
                    return Err(TransactionError::Protocol("invalid effective create mode"));
                }
                components(path)?;
            }
            Request::Unlink { path, .. } => {
                components(path)?;
            }
            Request::Rename {
                source,
                destination,
                ..
            } => {
                components(source)?;
                components(destination)?;
            }
        }
        self.revalidate_roots()?;
        let fingerprint = Self::request_fingerprint(&request);
        if let Some((prior, outcome)) = self.replay.get(&request.id()) {
            if prior == &fingerprint {
                return Ok(outcome.clone());
            }
            return Err(TransactionError::Protocol(
                "transaction id reused with different request",
            ));
        }
        if u64::from_ne_bytes(
            request.id().0[..8]
                .try_into()
                .expect("transaction generation"),
        ) != self.generation
        {
            return Err(TransactionError::Protocol("foreign transaction generation"));
        }
        if self.replay.len() == MAX_REPLAY {
            return Err(TransactionError::Protocol("transaction budget exhausted"));
        }
        if self.recovery.len() + self.garbage.len() >= MAX_RECOVERY {
            return Err(TransactionError::Protocol(
                "recovery storage budget exhausted",
            ));
        }
        self.journal_record(&format!(
            "B {} {}\n",
            Self::id_hex(request.id()),
            Self::hex(&fingerprint)
        ))?;
        let recovery_before = self.recovery.len();
        let outcome = match self.execute_once(&request) {
            Ok(outcome) => outcome,
            Err(_) => {
                self.active = false;
                let path = match &request {
                    Request::Create { path, .. }
                    | Request::Mkdir { path, .. }
                    | Request::Unlink { path, .. } => path.clone(),
                    Request::Rename { source, .. } => source.clone(),
                };
                self.recovery.push(RecoveryObligation::UnboundCreated {
                    path,
                    device: 0,
                    inode: 0,
                });
                Outcome::RecoveryRequired {
                    operation: "transaction error after durable begin",
                }
            }
        };
        if matches!(outcome, Outcome::RecoveryRequired { .. }) {
            self.active = false;
        }
        if let Outcome::Created { device, inode } = outcome {
            if let Some(fd) = self.pending_created.take() {
                self.created.insert(request.id(), fd);
            } else {
                let path = match &request {
                    Request::Create { path, .. } | Request::Mkdir { path, .. } => path.clone(),
                    _ => Vec::new(),
                };
                self.recovery.push(RecoveryObligation::UnboundCreated {
                    path,
                    device,
                    inode,
                });
                let recovery = Outcome::RecoveryRequired {
                    operation: "bind created inode",
                };
                self.remember(request, recovery.clone())?;
                return Ok(recovery);
            }
        } else if matches!(outcome, Outcome::RecoveryRequired { .. })
            && self.recovery.len() == recovery_before
        {
            if let Request::Create { path, .. } | Request::Mkdir { path, .. } = &request {
                self.recovery.push(RecoveryObligation::UnboundCreated {
                    path: path.clone(),
                    device: 0,
                    inode: 0,
                });
            }
        }
        let request_id = request.id();
        if let Err(error) = self.remember(request.clone(), outcome.clone()) {
            self.retain_commit_failure(request_id, &request, &outcome)?;
            return Err(error);
        }
        Ok(outcome)
    }

    fn retain_commit_failure(
        &mut self,
        id: TransactionId,
        request: &Request,
        outcome: &Outcome,
    ) -> Result<(), TransactionError> {
        self.active = false;
        if let Some(obligation) = self.pending_commit.remove(&id) {
            self.recovery.push(obligation);
            return Ok(());
        }
        if let Some(index) = self.garbage.iter().position(|entry| entry.id == id) {
            self.recovery.push(self.garbage.remove(index).obligation);
            return Ok(());
        }
        if let Some(object) = self.created.remove(&id) {
            let actual = identity(object.as_raw_fd())?;
            let path = match request {
                Request::Create { path, .. } | Request::Mkdir { path, .. } => path.clone(),
                _ => Vec::new(),
            };
            self.recovery.push(RecoveryObligation::BoundCreated {
                path,
                device: actual.device,
                inode: actual.inode,
                object,
            });
            return Ok(());
        }
        let (device, inode) = match outcome {
            Outcome::Created { device, inode } => (*device, *inode),
            _ => (0, 0),
        };
        let path = match request {
            Request::Create { path, .. }
            | Request::Mkdir { path, .. }
            | Request::Unlink { path, .. } => path.clone(),
            Request::Rename { destination, .. } => destination.clone(),
        };
        self.recovery.push(RecoveryObligation::UnboundCreated {
            path,
            device,
            inode,
        });
        Ok(())
    }

    fn revalidate_roots(&self) -> Result<(), TransactionError> {
        if identity(self.upper.as_raw_fd())? != self.upper_identity
            || identity(self.lower.as_raw_fd())? != self.lower_identity
        {
            return Err(TransactionError::IdentityMismatch);
        }
        Ok(())
    }

    fn remember(&mut self, request: Request, outcome: Outcome) -> Result<(), TransactionError> {
        let id = request.id();
        let fingerprint = Self::request_fingerprint(&request);
        self.journal_record(&format!(
            "C {} {} {}\n",
            Self::id_hex(id),
            Self::outcome_wire(&outcome),
            Self::hex(&fingerprint)
        ))?;
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterCommit);
        self.replay.insert(id, (fingerprint, outcome));
        self.pending_commit.remove(&id);
        Ok(())
    }

    fn request_fingerprint(request: &Request) -> Vec<u8> {
        let mut bytes = Vec::new();
        match request {
            Request::Create {
                path, flags, mode, ..
            } => {
                bytes.push(1);
                bytes.extend_from_slice(&flags.to_ne_bytes());
                bytes.extend_from_slice(&mode.to_ne_bytes());
                bytes.extend_from_slice(path);
            }
            Request::Mkdir { path, mode, .. } => {
                bytes.push(2);
                bytes.extend_from_slice(&0i32.to_ne_bytes());
                bytes.extend_from_slice(&mode.to_ne_bytes());
                bytes.extend_from_slice(path);
            }
            Request::Unlink { path, flags, .. } => {
                bytes.push(3);
                bytes.extend_from_slice(&flags.to_ne_bytes());
                bytes.extend_from_slice(&0u32.to_ne_bytes());
                bytes.extend_from_slice(path);
            }
            Request::Rename {
                source,
                destination,
                ..
            } => {
                bytes.push(4);
                bytes.extend_from_slice(&0i32.to_ne_bytes());
                bytes.extend_from_slice(&0u32.to_ne_bytes());
                bytes.extend_from_slice(&(source.len() as u16).to_ne_bytes());
                bytes.extend_from_slice(source);
                bytes.extend_from_slice(destination);
            }
        }
        bytes
    }

    fn hex(bytes: &[u8]) -> String {
        let mut output = String::with_capacity(bytes.len() * 2);
        use std::fmt::Write as _;
        for byte in bytes {
            write!(&mut output, "{byte:02x}").expect("hex");
        }
        output
    }

    fn unhex(value: &str) -> Option<Vec<u8>> {
        if !value.len().is_multiple_of(2) {
            return None;
        }
        (0..value.len())
            .step_by(2)
            .map(|index| u8::from_str_radix(&value[index..index + 2], 16).ok())
            .collect()
    }

    fn id_hex(id: TransactionId) -> String {
        Self::hex(&id.0)
    }

    fn parse_id(value: &str) -> Option<TransactionId> {
        let bytes = Self::unhex(value)?;
        TransactionId::new(bytes.try_into().ok()?).ok()
    }

    fn outcome_wire(outcome: &Outcome) -> String {
        match outcome {
            Outcome::Created { device, inode } => format!("created:{device}:{inode}"),
            Outcome::Mutated => "mutated".to_string(),
            Outcome::Rejected { errno, .. } => format!("rejected:{errno}"),
            Outcome::RecoveryRequired { .. } => "recovery".to_string(),
        }
    }

    fn parse_outcome(value: &str) -> Option<Outcome> {
        let fields: Vec<_> = value.split(':').collect();
        match fields.as_slice() {
            ["created", device, inode] => Some(Outcome::Created {
                device: device.parse().ok()?,
                inode: inode.parse().ok()?,
            }),
            ["mutated"] => Some(Outcome::Mutated),
            ["rejected", errno] => Some(Outcome::Rejected {
                errno: errno.parse().ok()?,
                reason: "durable rejection",
            }),
            ["recovery"] => Some(Outcome::RecoveryRequired {
                operation: "durable recovery",
            }),
            _ => None,
        }
    }

    fn fingerprint_primary_path(fingerprint: &[u8]) -> Option<Vec<u8>> {
        match fingerprint.first().copied()? {
            1..=3 => fingerprint.get(9..).map(ToOwned::to_owned),
            4 => {
                let length = u16::from_ne_bytes(fingerprint.get(9..11)?.try_into().ok()?) as usize;
                fingerprint.get(11..11 + length).map(ToOwned::to_owned)
            }
            _ => None,
        }
    }

    fn fingerprint_destination(fingerprint: &[u8]) -> Option<Vec<u8>> {
        if fingerprint.first().copied()? != 4 {
            return None;
        }
        let length = u16::from_ne_bytes(fingerprint.get(9..11)?.try_into().ok()?) as usize;
        fingerprint.get(11 + length..).map(ToOwned::to_owned)
    }

    fn journal_record(&mut self, record: &str) -> Result<(), TransactionError> {
        #[cfg(test)]
        let commit_fault = if record.starts_with("C ") {
            self.journal_fault.take()
        } else {
            None
        };
        #[cfg(test)]
        if commit_fault == Some(JournalFault::CommitWrite) {
            self.active = false;
            return Err(TransactionError::Io(
                "fault(commit WAL write)",
                io::Error::from_raw_os_error(libc::EIO),
            ));
        }
        let mut stat = MaybeUninit::<libc::stat>::uninit();
        if unsafe { libc::fstat(self.journal.as_raw_fd(), stat.as_mut_ptr()) } != 0 {
            self.active = false;
            return Err(io("fstat journal"));
        }
        if unsafe { stat.assume_init() }.st_size as usize + record.len() > MAX_JOURNAL_BYTES {
            self.active = false;
            return Err(TransactionError::Protocol("journal byte budget exhausted"));
        }
        let mut bytes = record.as_bytes();
        while !bytes.is_empty() {
            let written = unsafe {
                libc::write(self.journal.as_raw_fd(), bytes.as_ptr().cast(), bytes.len())
            };
            if written <= 0 {
                self.active = false;
                return Err(io("write transaction journal"));
            }
            bytes = &bytes[written as usize..];
        }
        #[cfg(test)]
        if commit_fault == Some(JournalFault::CommitFsync) {
            self.active = false;
            return Err(TransactionError::Io(
                "fault(commit WAL fsync)",
                io::Error::from_raw_os_error(libc::EIO),
            ));
        }
        if unsafe { libc::fsync(self.journal.as_raw_fd()) } != 0 {
            self.active = false;
            return Err(io("fsync transaction journal"));
        }
        Ok(())
    }

    fn recover_journal(&mut self) -> Result<(), TransactionError> {
        enum DurablePhase {
            Public(Vec<u8>, Identity),
            Quarantine(Vec<u8>, Identity),
        }
        let mut contents = vec![0u8; MAX_JOURNAL_BYTES + 1];
        let size = unsafe {
            libc::pread(
                self.journal.as_raw_fd(),
                contents.as_mut_ptr().cast(),
                contents.len(),
                0,
            )
        };
        if size < 0 {
            return Err(io("read transaction journal"));
        }
        if size as usize > MAX_JOURNAL_BYTES {
            return Err(TransactionError::Protocol("journal byte budget exceeded"));
        }
        contents.truncate(size as usize);
        if !contents.is_empty() && contents.last() != Some(&b'\n') {
            contents.truncate(
                contents
                    .iter()
                    .rposition(|byte| *byte == b'\n')
                    .map_or(0, |index| index + 1),
            );
            if unsafe { libc::ftruncate(self.journal.as_raw_fd(), contents.len() as libc::off_t) }
                != 0
                || unsafe { libc::fsync(self.journal.as_raw_fd()) } != 0
            {
                self.active = false;
                return Err(io("repair torn transaction journal tail"));
            }
        }
        let text = std::str::from_utf8(&contents)
            .map_err(|_| TransactionError::Protocol("journal encoding"))?;
        let mut begun = HashMap::<TransactionId, Vec<u8>>::new();
        let mut phases = HashMap::<TransactionId, DurablePhase>::new();
        let mut pending_gc = HashMap::<TransactionId, (Vec<u8>, Identity)>::new();
        for line in text.lines() {
            let fields: Vec<_> = line.split(' ').collect();
            match fields.as_slice() {
                ["B", id, fingerprint] => {
                    let id = Self::parse_id(id).ok_or(TransactionError::Protocol("journal id"))?;
                    if begun.contains_key(&id) || self.replay.contains_key(&id) {
                        return Err(TransactionError::Protocol("duplicate journal begin"));
                    }
                    begun.insert(
                        id,
                        Self::unhex(fingerprint)
                            .ok_or(TransactionError::Protocol("journal request"))?,
                    );
                }
                ["C", id, outcome, fingerprint] => {
                    let id = Self::parse_id(id).ok_or(TransactionError::Protocol("journal id"))?;
                    let fingerprint = Self::unhex(fingerprint)
                        .ok_or(TransactionError::Protocol("journal request"))?;
                    if begun.get(&id) != Some(&fingerprint) || self.replay.len() == MAX_REPLAY {
                        return Err(TransactionError::Protocol("journal commit without begin"));
                    }
                    let mut outcome = Self::parse_outcome(outcome)
                        .ok_or(TransactionError::Protocol("journal outcome"))?;
                    if let Outcome::Created { device, inode } = outcome {
                        let Some(DurablePhase::Public(path, expected)) = phases.get(&id) else {
                            return Err(TransactionError::Protocol(
                                "created journal commit lacks public identity",
                            ));
                        };
                        if (expected.device, expected.inode) != (device, inode) {
                            return Err(TransactionError::Protocol(
                                "created journal identity mismatch",
                            ));
                        }
                        let object = if let Some(parent) = self.resolve_parent(path)? {
                            Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?
                        } else {
                            None
                        };
                        if let Some(object) = object {
                            let actual = identity(object.as_raw_fd())?;
                            if actual == *expected {
                                self.created.insert(id, object);
                            } else {
                                self.recovery.push(RecoveryObligation::UnboundCreated {
                                    path: path.clone(),
                                    device,
                                    inode,
                                });
                                outcome = Outcome::RecoveryRequired {
                                    operation: "restart created identity mismatch",
                                };
                            }
                        } else {
                            self.recovery.push(RecoveryObligation::UnboundCreated {
                                path: path.clone(),
                                device,
                                inode,
                            });
                            outcome = Outcome::RecoveryRequired {
                                operation: "restart created inode missing",
                            };
                        }
                    }
                    let recovery_required = matches!(outcome, Outcome::RecoveryRequired { .. });
                    self.replay.insert(id, (fingerprint, outcome));
                    if !recovery_required {
                        begun.remove(&id);
                        phases.remove(&id);
                    }
                }
                ["P", id, path, device, inode] => {
                    let id = Self::parse_id(id).ok_or(TransactionError::Protocol("journal id"))?;
                    if !begun.contains_key(&id) || phases.contains_key(&id) {
                        return Err(TransactionError::Protocol("journal public phase order"));
                    }
                    phases.insert(
                        id,
                        DurablePhase::Public(
                            Self::unhex(path)
                                .ok_or(TransactionError::Protocol("journal public path"))?,
                            Identity {
                                device: device
                                    .parse()
                                    .map_err(|_| TransactionError::Protocol("journal device"))?,
                                inode: inode
                                    .parse()
                                    .map_err(|_| TransactionError::Protocol("journal inode"))?,
                            },
                        ),
                    );
                }
                ["Q", id, name, device, inode] => {
                    let id = Self::parse_id(id).ok_or(TransactionError::Protocol("journal id"))?;
                    if !begun.contains_key(&id) || phases.contains_key(&id) {
                        return Err(TransactionError::Protocol("journal quarantine phase order"));
                    }
                    let name_bytes = Self::unhex(name)
                        .ok_or(TransactionError::Protocol("journal quarantine"))?;
                    phases.insert(
                        id,
                        DurablePhase::Quarantine(
                            name_bytes,
                            Identity {
                                device: device
                                    .parse()
                                    .map_err(|_| TransactionError::Protocol("journal device"))?,
                                inode: inode
                                    .parse()
                                    .map_err(|_| TransactionError::Protocol("journal inode"))?,
                            },
                        ),
                    );
                }
                ["G", id, name, device, inode] => {
                    let id = Self::parse_id(id).ok_or(TransactionError::Protocol("journal id"))?;
                    if !begun.contains_key(&id) || pending_gc.contains_key(&id) {
                        return Err(TransactionError::Protocol("journal GC phase order"));
                    }
                    pending_gc.insert(
                        id,
                        (
                            Self::unhex(name)
                                .ok_or(TransactionError::Protocol("journal GC name"))?,
                            Identity {
                                device: device
                                    .parse()
                                    .map_err(|_| TransactionError::Protocol("journal device"))?,
                                inode: inode
                                    .parse()
                                    .map_err(|_| TransactionError::Protocol("journal inode"))?,
                            },
                        ),
                    );
                }
                ["D", id] => {
                    let id = Self::parse_id(id).ok_or(TransactionError::Protocol("journal id"))?;
                    if !self.replay.contains_key(&id) || pending_gc.remove(&id).is_none() {
                        return Err(TransactionError::Protocol(
                            "journal GC done without pending",
                        ));
                    }
                }
                _ => return Err(TransactionError::Protocol("journal record")),
            }
        }
        for (id, (name_bytes, expected)) in pending_gc {
            let name = CString::new(name_bytes.clone())
                .map_err(|_| TransactionError::Protocol("journal GC name"))?;
            match Self::open_leaf(self.quarantine.as_raw_fd(), &name)? {
                Some(object) => {
                    let actual = identity(object.as_raw_fd())?;
                    if actual == expected {
                        self.garbage.push(GarbageObligation {
                            id,
                            obligation: RecoveryObligation::BoundQuarantine {
                                operation: "restart garbage collection",
                                parent: duplicate(self.quarantine.as_raw_fd())?,
                                staged_name: name_bytes,
                                device: actual.device,
                                inode: actual.inode,
                                object,
                            },
                        });
                    } else {
                        self.recovery.push(RecoveryObligation::BoundQuarantine {
                            operation: "restart GC replacement",
                            parent: duplicate(self.quarantine.as_raw_fd())?,
                            staged_name: name_bytes,
                            device: actual.device,
                            inode: actual.inode,
                            object,
                        });
                    }
                }
                None => self.journal_record(&format!("D {}\n", Self::id_hex(id)))?,
            }
        }
        for (id, fingerprint) in &begun {
            match phases.remove(id) {
                Some(DurablePhase::Quarantine(name_bytes, expected)) => {
                    let name = CString::new(name_bytes.clone())
                        .map_err(|_| TransactionError::Protocol("journal quarantine"))?;
                    if let Some(object) = Self::open_leaf(self.quarantine.as_raw_fd(), &name)? {
                        let actual = identity(object.as_raw_fd())?;
                        if actual == expected {
                            self.recovery.push(RecoveryObligation::BoundQuarantine {
                                operation: "restart recovery",
                                parent: duplicate(self.quarantine.as_raw_fd())?,
                                staged_name: name_bytes,
                                device: actual.device,
                                inode: actual.inode,
                                object,
                            });
                        }
                    } else if let Some(path) = Self::fingerprint_primary_path(fingerprint) {
                        let mut recovered = false;
                        if let Some(parent) = self.resolve_parent(&path)? {
                            if let Some(object) =
                                Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?
                            {
                                let actual = identity(object.as_raw_fd())?;
                                if actual == expected {
                                    self.recovery.push(RecoveryObligation::BoundCreated {
                                        path,
                                        device: actual.device,
                                        inode: actual.inode,
                                        object,
                                    });
                                    recovered = true;
                                }
                            }
                        }
                        if !recovered {
                            let Some(destination) = Self::fingerprint_destination(fingerprint)
                            else {
                                continue;
                            };
                            if let Some(parent) = self.resolve_parent(&destination)? {
                                if let Some(object) =
                                    Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?
                                {
                                    let actual = identity(object.as_raw_fd())?;
                                    if actual == expected {
                                        self.recovery.push(RecoveryObligation::BoundCreated {
                                            path: destination,
                                            device: actual.device,
                                            inode: actual.inode,
                                            object,
                                        });
                                    }
                                }
                            }
                        }
                    }
                }
                Some(DurablePhase::Public(path, expected)) => {
                    if let Some(parent) = self.resolve_parent(&path)? {
                        if let Some(object) =
                            Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?
                        {
                            let actual = identity(object.as_raw_fd())?;
                            if actual == expected {
                                self.recovery.push(RecoveryObligation::BoundCreated {
                                    path,
                                    device: actual.device,
                                    inode: actual.inode,
                                    object,
                                });
                            }
                        }
                    }
                }
                None => {
                    let path = Self::fingerprint_primary_path(fingerprint).unwrap_or_default();
                    let actual = if let Some(parent) = self.resolve_parent(&path)? {
                        Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?
                            .and_then(|fd| identity(fd.as_raw_fd()).ok())
                    } else {
                        None
                    };
                    self.recovery.push(RecoveryObligation::UnboundCreated {
                        path,
                        device: actual.map_or(0, |identity| identity.device),
                        inode: actual.map_or(0, |identity| identity.inode),
                    });
                }
            }
        }
        if !begun.is_empty() || !self.recovery.is_empty() {
            self.active = false;
        }
        Ok(())
    }

    fn resolve_parent(&self, path: &[u8]) -> Result<Option<ResolvedParent>, TransactionError> {
        let mut parts = components(path)?;
        let leaf = parts
            .pop()
            .ok_or(TransactionError::Protocol("missing leaf"))?;
        let mut upper = duplicate(self.upper.as_raw_fd())?;
        let mut lower = Some(duplicate(self.lower.as_raw_fd())?);
        let mut logical_parts = Vec::new();
        for part in parts {
            upper = match open_dir(upper.as_raw_fd(), &part) {
                Ok(directory) => directory,
                Err(TransactionError::Io(_, error))
                    if error.raw_os_error() == Some(libc::ENOENT) =>
                {
                    return Ok(None)
                }
                Err(error) => return Err(error),
            };
            logical_parts.push(part);
            lower = match open_logical_dir(self.lower.as_raw_fd(), &logical_parts) {
                Ok(directory) => Some(directory),
                Err(TransactionError::Io(_, error))
                    if error.raw_os_error() == Some(libc::ENOENT) =>
                {
                    None
                }
                Err(error) => return Err(error),
            };
        }
        Ok(Some(ResolvedParent { upper, lower, leaf }))
    }

    fn open_leaf(parent: RawFd, leaf: &CStr) -> Result<Option<OwnedFd>, TransactionError> {
        let fd = unsafe {
            libc::openat(
                parent,
                leaf.as_ptr(),
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd >= 0 {
            return Ok(Some(unsafe { OwnedFd::from_raw_fd(fd) }));
        }
        let error = io::Error::last_os_error();
        if error.raw_os_error() == Some(libc::ENOENT) {
            Ok(None)
        } else {
            Err(TransactionError::Io("open retained leaf", error))
        }
    }

    fn is_whiteout(fd: RawFd) -> Result<bool, TransactionError> {
        let path = CString::new(format!("/proc/self/fd/{fd}"))
            .map_err(|_| TransactionError::Protocol("whiteout fd path"))?;
        let name = c"user.union.whiteout";
        let mut value = [0u8; 8];
        let size = unsafe {
            libc::getxattr(
                path.as_ptr(),
                name.as_ptr(),
                value.as_mut_ptr().cast(),
                value.len(),
            )
        };
        if size >= 0 {
            return Ok(true);
        }
        let error = io::Error::last_os_error();
        if matches!(
            error.raw_os_error(),
            Some(libc::ENODATA) | Some(libc::ENOTSUP)
        ) {
            Ok(false)
        } else {
            Err(TransactionError::Io("getxattr whiteout", error))
        }
    }

    fn classify(parent: &ResolvedParent) -> Result<LayerState, TransactionError> {
        let upper_leaf = Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?;
        if let Some(fd) = upper_leaf.as_ref() {
            if Self::is_whiteout(fd.as_raw_fd())? {
                return Ok(LayerState::Whiteout);
            }
        }
        let upper = upper_leaf.is_some();
        let lower = match &parent.lower {
            Some(fd) => exists(fd.as_raw_fd(), &parent.leaf)?,
            None => false,
        };
        Ok(match (upper, lower) {
            (false, false) => LayerState::Absent,
            (true, false) => LayerState::UpperOnly,
            (false, true) => LayerState::LowerOnly,
            (true, true) => LayerState::Both,
        })
    }

    fn classify_create(parent: &ResolvedParent) -> Result<LayerState, TransactionError> {
        if let Some(fd) = Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)? {
            return if Self::is_whiteout(fd.as_raw_fd())? {
                Ok(LayerState::Whiteout)
            } else {
                // O_CREAT|O_EXCL and mkdir fail with EEXIST as soon as the
                // writable name exists. Resolving a lower namesake (whose
                // parent may itself be a relative symlink such as /var) is
                // unnecessary and must not turn that deterministic result into
                // a recovery obligation.
                Ok(LayerState::UpperOnly)
            };
        }
        Self::classify(parent)
    }

    fn execute_once(&mut self, request: &Request) -> Result<Outcome, TransactionError> {
        match request {
            Request::Create {
                id,
                path,
                flags,
                mode,
            } => self.create(*id, path, *flags, *mode, false),
            Request::Mkdir { id, path, mode } => self.create(*id, path, 0, *mode, true),
            Request::Unlink { path, flags, id } => self.unlink(*id, path, *flags),
            Request::Rename {
                source,
                destination,
                ..
            } => self.rename(request.id(), source, destination),
        }
    }

    fn create(
        &mut self,
        request_id: TransactionId,
        path: &[u8],
        flags: i32,
        mode: u32,
        directory: bool,
    ) -> Result<Outcome, TransactionError> {
        let Some(parent) = self.resolve_parent(path)? else {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "upper parent unavailable",
            });
        };
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterResolve);
        match Self::classify_create(&parent)? {
            LayerState::Absent => {}
            LayerState::UpperOnly | LayerState::LowerOnly | LayerState::Both => {
                return Ok(Outcome::Rejected {
                    errno: libc::EEXIST,
                    reason: "merged target already exists",
                });
            }
            LayerState::Whiteout => {
                return Ok(Outcome::Rejected {
                    errno: libc::ENOTSUP,
                    reason: "unsupported non-upper E-UNION target",
                });
            }
        }
        let created_fd = if directory {
            if unsafe { libc::mkdirat(parent.upper.as_raw_fd(), parent.leaf.as_ptr(), 0o700) } != 0
            {
                return Err(io("mkdirat transaction"));
            }
            if unsafe {
                libc::fchmodat(
                    parent.upper.as_raw_fd(),
                    parent.leaf.as_ptr(),
                    mode as libc::mode_t,
                    0,
                )
            } != 0
            {
                return Err(io("apply directory guest effective mode"));
            }
            let fd = unsafe {
                libc::openat(
                    parent.upper.as_raw_fd(),
                    parent.leaf.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if fd < 0 {
                return Ok(Outcome::RecoveryRequired {
                    operation: "bind created directory",
                });
            }
            unsafe { OwnedFd::from_raw_fd(fd) }
        } else {
            let allowed =
                libc::O_ACCMODE | libc::O_APPEND | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC;
            if flags & !allowed != 0
                || flags & (libc::O_CREAT | libc::O_EXCL) != (libc::O_CREAT | libc::O_EXCL)
                || flags & libc::O_CLOEXEC == 0
            {
                return Ok(Outcome::Rejected {
                    errno: libc::ENOTSUP,
                    reason: "unsupported create flags",
                });
            }
            let fd = unsafe {
                libc::openat(
                    parent.upper.as_raw_fd(),
                    parent.leaf.as_ptr(),
                    libc::O_CREAT
                        | libc::O_EXCL
                        | (flags & (libc::O_ACCMODE | libc::O_APPEND))
                        | libc::O_CLOEXEC
                        | libc::O_NOFOLLOW,
                    0,
                )
            };
            if fd < 0 {
                return Err(io("openat create transaction"));
            }
            unsafe { OwnedFd::from_raw_fd(fd) }
        };
        let created_identity = match identity(created_fd.as_raw_fd()) {
            Ok(value) => value,
            Err(_) => {
                self.active = false;
                self.recovery.push(RecoveryObligation::UnboundCreated {
                    path: path.to_vec(),
                    device: 0,
                    inode: 0,
                });
                return Ok(Outcome::RecoveryRequired {
                    operation: "identify created inode",
                });
            }
        };
        if Self::fsync_directory(parent.upper.as_raw_fd()).is_err() {
            self.active = false;
            self.recovery.push(RecoveryObligation::BoundCreated {
                path: path.to_vec(),
                device: created_identity.device,
                inode: created_identity.inode,
                object: created_fd,
            });
            return Ok(Outcome::RecoveryRequired {
                operation: "persist created directory entry",
            });
        }
        if self
            .journal_record(&format!(
                "P {} {} {} {}\n",
                Self::id_hex(request_id),
                Self::hex(path),
                created_identity.device,
                created_identity.inode
            ))
            .is_err()
        {
            self.active = false;
            self.recovery.push(RecoveryObligation::BoundCreated {
                path: path.to_vec(),
                device: created_identity.device,
                inode: created_identity.inode,
                object: created_fd,
            });
            return Ok(Outcome::RecoveryRequired {
                operation: "persist created inode",
            });
        }
        if unsafe { libc::fchmod(created_fd.as_raw_fd(), mode as libc::mode_t) } != 0 {
            self.active = false;
            self.recovery.push(RecoveryObligation::BoundCreated {
                path: path.to_vec(),
                device: created_identity.device,
                inode: created_identity.inode,
                object: created_fd,
            });
            return Ok(Outcome::RecoveryRequired {
                operation: "apply guest effective mode",
            });
        }
        self.pending_created = Some(created_fd);
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterMutation);
        let named_matches = {
            let mut stat = MaybeUninit::<libc::stat>::uninit();
            (unsafe {
                libc::fstatat(
                    parent.upper.as_raw_fd(),
                    parent.leaf.as_ptr(),
                    stat.as_mut_ptr(),
                    libc::AT_SYMLINK_NOFOLLOW,
                )
            }) == 0
                && {
                    let stat = unsafe { stat.assume_init() };
                    stat.st_dev == created_identity.device && stat.st_ino == created_identity.inode
                }
        };
        if !named_matches {
            let object = self
                .pending_created
                .take()
                .ok_or(TransactionError::Protocol(
                    "created capability missing after mutation",
                ))?;
            self.recovery.push(RecoveryObligation::BoundCreated {
                path: path.to_vec(),
                device: created_identity.device,
                inode: created_identity.inode,
                object,
            });
            return Ok(Outcome::RecoveryRequired {
                operation: "created name replaced",
            });
        }
        Ok(Outcome::Created {
            device: created_identity.device,
            inode: created_identity.inode,
        })
    }

    fn quarantine_name(id: TransactionId, suffix: &str) -> Result<CString, TransactionError> {
        let mut encoded = String::with_capacity(48);
        use std::fmt::Write as _;
        for byte in id.0 {
            write!(&mut encoded, "{byte:02x}").expect("string write");
        }
        CString::new(format!(".darling-txn-{encoded}-{suffix}"))
            .map_err(|_| TransactionError::Protocol("quarantine name"))
    }

    fn retain_quarantine(
        &mut self,
        id: TransactionId,
        operation: &'static str,
        parent: &OwnedFd,
        name: &CStr,
        object: OwnedFd,
    ) -> Result<(), TransactionError> {
        let value = identity(object.as_raw_fd())?;
        self.garbage.push(GarbageObligation {
            id,
            obligation: RecoveryObligation::BoundQuarantine {
                operation,
                parent: duplicate(parent.as_raw_fd())?,
                staged_name: name.to_bytes().to_vec(),
                device: value.device,
                inode: value.inode,
                object,
            },
        });
        Ok(())
    }

    fn retain_recovery_quarantine(
        &mut self,
        operation: &'static str,
        parent: &OwnedFd,
        name: &CStr,
        object: OwnedFd,
    ) -> Result<(), TransactionError> {
        let value = identity(object.as_raw_fd())?;
        self.recovery.push(RecoveryObligation::BoundQuarantine {
            operation,
            parent: duplicate(parent.as_raw_fd())?,
            staged_name: name.to_bytes().to_vec(),
            device: value.device,
            inode: value.inode,
            object,
        });
        Ok(())
    }

    fn retain_displaced_recovery(
        &mut self,
        operation: &'static str,
        original_path: &[u8],
        parent: &OwnedFd,
        staged: &CStr,
        original: OwnedFd,
    ) -> Result<(), TransactionError> {
        let original_identity = identity(original.as_raw_fd())?;
        self.recovery.push(RecoveryObligation::DisplacedSource {
            operation,
            original_path: original_path.to_vec(),
            device: original_identity.device,
            inode: original_identity.inode,
            object: original,
        });
        if let Some(replacement) = Self::open_leaf(parent.as_raw_fd(), staged)? {
            self.retain_recovery_quarantine(operation, parent, staged, replacement)?;
        }
        Ok(())
    }

    fn collect_garbage(&mut self) {
        let garbage = std::mem::take(&mut self.garbage);
        for GarbageObligation { id, obligation } in garbage {
            let RecoveryObligation::BoundQuarantine {
                operation,
                parent,
                staged_name,
                device,
                inode,
                object,
            } = obligation
            else {
                self.recovery.push(obligation);
                continue;
            };
            let Ok(name) = CString::new(staged_name.clone()) else {
                self.recovery.push(RecoveryObligation::BoundQuarantine {
                    operation,
                    parent,
                    staged_name,
                    device,
                    inode,
                    object,
                });
                continue;
            };
            let exact = Self::open_leaf(parent.as_raw_fd(), &name)
                .ok()
                .flatten()
                .and_then(|fd| identity(fd.as_raw_fd()).ok())
                .is_some_and(|actual| actual.device == device && actual.inode == inode);
            if !exact {
                self.recovery.push(RecoveryObligation::DisplacedSource {
                    operation,
                    original_path: staged_name.clone(),
                    device,
                    inode,
                    object,
                });
                if let Ok(Some(replacement)) = Self::open_leaf(parent.as_raw_fd(), &name) {
                    if let Ok(actual) = identity(replacement.as_raw_fd()) {
                        self.recovery.push(RecoveryObligation::BoundQuarantine {
                            operation,
                            parent,
                            staged_name,
                            device: actual.device,
                            inode: actual.inode,
                            object: replacement,
                        });
                    }
                }
            } else if unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), 0) } != 0 {
                self.recovery.push(RecoveryObligation::BoundQuarantine {
                    operation,
                    parent,
                    staged_name,
                    device,
                    inode,
                    object,
                });
            } else if Self::fsync_directory(parent.as_raw_fd()).is_err()
                || self
                    .journal_record(&format!("D {}\n", Self::id_hex(id)))
                    .is_err()
            {
                self.active = false;
                self.recovery.push(RecoveryObligation::UnboundCreated {
                    path: staged_name,
                    device,
                    inode,
                });
            }
        }
    }

    fn unlink(
        &mut self,
        id: TransactionId,
        path: &[u8],
        flags: i32,
    ) -> Result<Outcome, TransactionError> {
        if flags != 0 {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "unsupported unlink flags",
            });
        }
        let Some(parent) = self.resolve_parent(path)? else {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "upper parent unavailable",
            });
        };
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterResolve);
        if Self::classify(&parent)? != LayerState::UpperOnly {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "unlink requires upper-only source",
            });
        }
        let object = Self::open_leaf(parent.upper.as_raw_fd(), &parent.leaf)?
            .ok_or(TransactionError::Protocol("classified leaf disappeared"))?;
        let expected = identity(object.as_raw_fd())?;
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterLeafAuthority);
        let quarantine_parent = duplicate(self.quarantine.as_raw_fd())?;
        let staged = Self::quarantine_name(id, "unlink")?;
        self.journal_record(&format!(
            "Q {} {} {} {}\n",
            Self::id_hex(id),
            Self::hex(staged.to_bytes()),
            expected.device,
            expected.inode
        ))?;
        let rc = unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                parent.upper.as_raw_fd(),
                parent.leaf.as_ptr(),
                quarantine_parent.as_raw_fd(),
                staged.as_ptr(),
                libc::RENAME_NOREPLACE,
            ) as c_int
        };
        if rc != 0 {
            return Err(io("quarantine unlink source"));
        }
        if Self::fsync_directory(parent.upper.as_raw_fd()).is_err()
            || Self::fsync_directory(quarantine_parent.as_raw_fd()).is_err()
        {
            self.retain_recovery_quarantine(
                "persist unlink quarantine move",
                &quarantine_parent,
                &staged,
                object,
            )?;
            return Ok(Outcome::RecoveryRequired {
                operation: "persist unlink quarantine move",
            });
        }
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterQuarantineMove);
        let staged_actual = Self::open_leaf(quarantine_parent.as_raw_fd(), &staged)?
            .and_then(|fd| identity(fd.as_raw_fd()).ok());
        if !staged_actual.is_some_and(|actual| actual == expected) {
            self.retain_displaced_recovery(
                "unlink source replacement",
                path,
                &quarantine_parent,
                &staged,
                object,
            )?;
            return Ok(Outcome::RecoveryRequired {
                operation: "unlink source replacement",
            });
        }
        self.journal_record(&format!(
            "G {} {} {} {}\n",
            Self::id_hex(id),
            Self::hex(staged.to_bytes()),
            expected.device,
            expected.inode
        ))?;
        self.retain_quarantine(
            id,
            "unlink garbage collection",
            &quarantine_parent,
            &staged,
            object,
        )?;
        Ok(Outcome::Mutated)
    }

    fn rename(
        &mut self,
        id: TransactionId,
        source: &[u8],
        destination: &[u8],
    ) -> Result<Outcome, TransactionError> {
        let source_path = source.to_vec();
        let destination_path = destination.to_vec();
        let Some(source) = self.resolve_parent(source)? else {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "upper source parent unavailable",
            });
        };
        let Some(destination) = self.resolve_parent(destination)? else {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "upper destination parent unavailable",
            });
        };
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterResolve);
        if Self::classify(&source)? != LayerState::UpperOnly
            || Self::classify(&destination)? != LayerState::Absent
        {
            return Ok(Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "rename requires upper-only source and absent destination",
            });
        }
        let object = Self::open_leaf(source.upper.as_raw_fd(), &source.leaf)?
            .ok_or(TransactionError::Protocol("classified source disappeared"))?;
        let expected = identity(object.as_raw_fd())?;
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterLeafAuthority);
        let quarantine_parent = duplicate(self.quarantine.as_raw_fd())?;
        let staged = Self::quarantine_name(id, "rename")?;
        self.journal_record(&format!(
            "Q {} {} {} {}\n",
            Self::id_hex(id),
            Self::hex(staged.to_bytes()),
            expected.device,
            expected.inode
        ))?;
        let staged_rc = unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                source.upper.as_raw_fd(),
                source.leaf.as_ptr(),
                quarantine_parent.as_raw_fd(),
                staged.as_ptr(),
                libc::RENAME_NOREPLACE,
            ) as c_int
        };
        if staged_rc != 0 {
            return Err(io("quarantine rename source"));
        }
        if Self::fsync_directory(source.upper.as_raw_fd()).is_err()
            || Self::fsync_directory(quarantine_parent.as_raw_fd()).is_err()
        {
            self.retain_recovery_quarantine(
                "persist rename quarantine move",
                &quarantine_parent,
                &staged,
                object,
            )?;
            return Ok(Outcome::RecoveryRequired {
                operation: "persist rename quarantine move",
            });
        }
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterQuarantineMove);
        let staged_actual = Self::open_leaf(quarantine_parent.as_raw_fd(), &staged)?
            .and_then(|fd| identity(fd.as_raw_fd()).ok());
        if !staged_actual.is_some_and(|actual| actual == expected) {
            self.retain_displaced_recovery(
                "rename source replacement",
                &source_path,
                &quarantine_parent,
                &staged,
                object,
            )?;
            return Ok(Outcome::RecoveryRequired {
                operation: "rename source replacement",
            });
        }
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterQuarantineVerify);
        let rc = unsafe {
            libc::syscall(
                libc::SYS_renameat2,
                quarantine_parent.as_raw_fd(),
                staged.as_ptr(),
                destination.upper.as_raw_fd(),
                destination.leaf.as_ptr(),
                libc::RENAME_NOREPLACE,
            ) as c_int
        };
        if rc != 0 {
            self.retain_recovery_quarantine(
                "rename publication",
                &quarantine_parent,
                &staged,
                object,
            )?;
            return Ok(Outcome::RecoveryRequired {
                operation: "rename publication",
            });
        }
        if Self::fsync_directory(quarantine_parent.as_raw_fd()).is_err()
            || Self::fsync_directory(destination.upper.as_raw_fd()).is_err()
        {
            let actual = identity(object.as_raw_fd())?;
            self.recovery.push(RecoveryObligation::BoundCreated {
                path: destination_path,
                device: actual.device,
                inode: actual.inode,
                object,
            });
            return Ok(Outcome::RecoveryRequired {
                operation: "persist rename publication",
            });
        }
        #[cfg(test)]
        self.checkpoint(TestCheckpoint::AfterPublication);
        let published = Self::open_leaf(destination.upper.as_raw_fd(), &destination.leaf)?;
        let published_identity = published
            .as_ref()
            .and_then(|fd| identity(fd.as_raw_fd()).ok());
        if !published_identity.is_some_and(|actual| actual == expected) {
            let original_identity = identity(object.as_raw_fd())?;
            self.recovery.push(RecoveryObligation::DisplacedSource {
                operation: "rename publication replacement",
                original_path: source_path,
                device: original_identity.device,
                inode: original_identity.inode,
                object,
            });
            if let Some(replacement) = published {
                self.retain_recovery_quarantine(
                    "rename publication replacement",
                    &destination.upper,
                    &destination.leaf,
                    replacement,
                )?;
            }
            return Ok(Outcome::RecoveryRequired {
                operation: "rename publication replacement",
            });
        }
        self.pending_commit.insert(
            id,
            RecoveryObligation::BoundCreated {
                path: destination_path,
                device: expected.device,
                inode: expected.inode,
                object,
            },
        );
        Ok(Outcome::Mutated)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::ffi::OsStrExt;
    use std::os::unix::fs::PermissionsExt;
    use std::path::{Path, PathBuf};

    struct Roots {
        root: PathBuf,
        upper: OwnedFd,
        lower: OwnedFd,
        sidecar: OwnedFd,
    }
    impl Roots {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "darling-guest-txn-{}-{}",
                unsafe { libc::getpid() },
                NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
            ));
            fs::create_dir_all(root.join("upper/dir")).unwrap();
            fs::create_dir_all(root.join("lower/dir")).unwrap();
            fs::create_dir(root.join("sidecar")).unwrap();
            fs::set_permissions(root.join("sidecar"), fs::Permissions::from_mode(0o700)).unwrap();
            Self {
                upper: open_root(&root.join("upper")),
                lower: open_root(&root.join("lower")),
                sidecar: open_root(&root.join("sidecar")),
                root,
            }
        }
        fn service(&self) -> GuestNamespaceTransactionService {
            GuestNamespaceTransactionService::from_retained_roots(
                self.upper.as_raw_fd(),
                self.lower.as_raw_fd(),
                self.sidecar.as_raw_fd(),
                7,
            )
            .unwrap()
        }
    }
    impl Drop for Roots {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(1);
    fn open_root(path: &Path) -> OwnedFd {
        let path = CString::new(path.as_os_str().as_bytes()).unwrap();
        let fd = unsafe {
            libc::open(
                path.as_ptr(),
                libc::O_PATH | libc::O_DIRECTORY | libc::O_CLOEXEC,
            )
        };
        assert!(fd >= 0);
        unsafe { OwnedFd::from_raw_fd(fd) }
    }
    fn id(value: u8) -> TransactionId {
        id64(u64::from(value))
    }
    fn id64(value: u64) -> TransactionId {
        let mut bytes = [0; 16];
        bytes[..8].copy_from_slice(&7u64.to_ne_bytes());
        bytes[8..].copy_from_slice(&value.to_ne_bytes());
        TransactionId::new(bytes).unwrap()
    }

    #[test]
    fn create_is_idempotent_and_reuse_mismatch_is_rejected() {
        let roots = Roots::new();
        let mut service = roots.service();
        let request = Request::Create {
            id: id(1),
            path: b"dir/file".to_vec(),
            flags: libc::O_WRONLY | libc::O_APPEND | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            mode: 0o600,
        };
        let first = service.execute(request.clone()).unwrap();
        assert_eq!(service.execute(request).unwrap(), first);
        let Outcome::Created { device, inode } = first else {
            panic!("create did not return identity")
        };
        let first_fd = service
            .duplicate_created_result(id(1), device, inode)
            .unwrap();
        let replay_fd = service
            .duplicate_created_result(id(1), device, inode)
            .unwrap();
        assert_ne!(first_fd.as_raw_fd(), replay_fd.as_raw_fd());
        assert_eq!(identity(first_fd.as_raw_fd()).unwrap().inode, inode);
        assert_eq!(identity(replay_fd.as_raw_fd()).unwrap().inode, inode);
        assert!(matches!(
            service.execute(Request::Mkdir {
                id: id(1),
                path: b"dir/other".to_vec(),
                mode: 0o700
            }),
            Err(TransactionError::Protocol(_))
        ));
    }

    #[test]
    fn lower_and_both_layer_mutations_fail_before_change() {
        let roots = Roots::new();
        fs::write(roots.root.join("lower/dir/lower"), b"lower").unwrap();
        fs::write(roots.root.join("lower/dir/both"), b"lower").unwrap();
        fs::write(roots.root.join("upper/dir/both"), b"upper").unwrap();
        let mut service = roots.service();
        for (value, path) in [(2, b"dir/lower".as_slice()), (3, b"dir/both".as_slice())] {
            assert_eq!(
                service
                    .execute(Request::Unlink {
                        id: id(value),
                        path: path.to_vec(),
                        flags: 0,
                    })
                    .unwrap(),
                Outcome::Rejected {
                    errno: libc::ENOTSUP,
                    reason: "unlink requires upper-only source"
                }
            );
        }
        assert_eq!(
            fs::read(roots.root.join("lower/dir/lower")).unwrap(),
            b"lower"
        );
        assert_eq!(
            fs::read(roots.root.join("upper/dir/both")).unwrap(),
            b"upper"
        );
    }

    #[test]
    fn lower_only_parent_is_typed_unsupported_before_mutation() {
        let roots = Roots::new();
        fs::create_dir(roots.root.join("lower/lower-parent")).unwrap();
        let mut service = roots.service();
        assert_eq!(
            service
                .execute(Request::Create {
                    id: id(10),
                    path: b"lower-parent/child".to_vec(),
                    flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                    mode: 0o600,
                })
                .unwrap(),
            Outcome::Rejected {
                errno: libc::ENOTSUP,
                reason: "upper parent unavailable"
            }
        );
        assert!(!roots.root.join("upper/lower-parent").exists());
        assert!(!roots.root.join("lower/lower-parent/child").exists());
    }

    #[test]
    fn upper_only_unlink_and_rename_are_exact() {
        let roots = Roots::new();
        fs::write(roots.root.join("upper/dir/unlink"), b"u").unwrap();
        fs::write(roots.root.join("upper/dir/source"), b"r").unwrap();
        let mut service = roots.service();
        assert_eq!(
            service
                .execute(Request::Unlink {
                    id: id(4),
                    path: b"dir/unlink".to_vec(),
                    flags: 0,
                })
                .unwrap(),
            Outcome::Mutated
        );
        assert_eq!(
            service
                .execute(Request::Rename {
                    id: id(5),
                    source: b"dir/source".to_vec(),
                    destination: b"dir/destination".to_vec()
                })
                .unwrap(),
            Outcome::Mutated
        );
        assert!(!roots.root.join("upper/dir/unlink").exists());
        assert_eq!(
            fs::read(roots.root.join("upper/dir/destination")).unwrap(),
            b"r"
        );
        service.revoke();
        assert!(service.recovery_obligations().is_empty());
        assert!(fs::read_dir(roots.root.join("upper/dir"))
            .unwrap()
            .all(|entry| !entry
                .unwrap()
                .file_name()
                .as_bytes()
                .starts_with(b".darling-txn-")));
    }

    #[test]
    fn revoked_and_unbounded_paths_fail_closed() {
        let roots = Roots::new();
        let mut service = roots.service();
        service.revoke();
        assert!(matches!(
            service.execute(Request::Create {
                id: id(6),
                path: b"dir/x".to_vec(),
                flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                mode: 0o600
            }),
            Err(TransactionError::Revoked)
        ));
        let mut service = roots.service();
        assert!(matches!(
            service.execute(Request::Create {
                id: id(7),
                path: vec![b'x'; MAX_PATH_BYTES + 1],
                flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                mode: 0o600
            }),
            Err(TransactionError::Protocol(_))
        ));
    }

    #[test]
    fn parent_swap_after_resolve_mutates_retained_parent_only() {
        let roots = Roots::new();
        let original = roots.root.join("upper/dir");
        let retained = roots.root.join("upper/dir.retained");
        let replacement = roots.root.join("upper/dir");
        let hook_original = original.clone();
        let hook_retained = retained.clone();
        let mut service = roots.service();
        service.set_test_hook(move |checkpoint| {
            if checkpoint == TestCheckpoint::AfterResolve && !hook_retained.exists() {
                fs::rename(&hook_original, &hook_retained).unwrap();
                fs::create_dir(&hook_original).unwrap();
                fs::write(hook_original.join("sentinel"), b"replacement").unwrap();
            }
        });
        let outcome = service
            .execute(Request::Create {
                id: id(8),
                path: b"dir/created".to_vec(),
                flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                mode: 0o600,
            })
            .unwrap();
        assert!(matches!(outcome, Outcome::Created { .. }));
        assert!(retained.join("created").exists());
        assert_eq!(
            fs::read(replacement.join("sentinel")).unwrap(),
            b"replacement"
        );
        assert!(!replacement.join("created").exists());
    }

    #[test]
    fn replacement_after_mutation_retains_exact_fd_and_requires_recovery() {
        let roots = Roots::new();
        let created = roots.root.join("upper/dir/created");
        let saved = roots.root.join("upper/dir/created.retained");
        let hook_created = created.clone();
        let hook_saved = saved.clone();
        let mut service = roots.service();
        service.set_test_hook(move |checkpoint| {
            if checkpoint == TestCheckpoint::AfterMutation {
                fs::rename(&hook_created, &hook_saved).unwrap();
                fs::write(&hook_created, b"replacement").unwrap();
            }
        });
        assert_eq!(
            service
                .execute(Request::Create {
                    id: id(9),
                    path: b"dir/created".to_vec(),
                    flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                    mode: 0o600,
                })
                .unwrap(),
            Outcome::RecoveryRequired {
                operation: "created name replaced"
            }
        );
        assert_eq!(service.recovery_obligations().len(), 1);
        let RecoveryObligation::BoundCreated { object, inode, .. } =
            &service.recovery_obligations()[0]
        else {
            panic!("expected bound recovery obligation")
        };
        assert_eq!(identity(object.as_raw_fd()).unwrap().inode, *inode);
        assert_eq!(fs::read(&created).unwrap(), b"replacement");
        assert_eq!(fs::read(&saved).unwrap(), b"");
    }

    #[test]
    fn whiteout_is_typed_and_rejected_before_mutation() {
        let roots = Roots::new();
        let marker = roots.root.join("upper/dir/marker");
        fs::write(&marker, b"whiteout").unwrap();
        let marker_c = CString::new(marker.as_os_str().as_bytes()).unwrap();
        assert_eq!(
            unsafe {
                libc::setxattr(
                    marker_c.as_ptr(),
                    c"user.union.whiteout".as_ptr(),
                    b"y".as_ptr().cast(),
                    1,
                    0,
                )
            },
            0
        );
        let mut service = roots.service();
        let outcome = service
            .execute(Request::Unlink {
                id: id(11),
                path: b"dir/marker".to_vec(),
                flags: 0,
            })
            .unwrap();
        assert!(matches!(
            outcome,
            Outcome::Rejected {
                errno: libc::ENOTSUP,
                ..
            }
        ));
        assert_eq!(fs::read(marker).unwrap(), b"whiteout");
    }

    #[test]
    fn replay_tombstones_are_bounded_without_eviction_or_reexecution() {
        let roots = Roots::new();
        let mut service = roots.service();
        let first = Request::Mkdir {
            id: id64(1),
            path: b"dir/one".to_vec(),
            mode: 0o700,
        };
        let first_outcome = service.execute(first.clone()).unwrap();
        for value in 2..=MAX_REPLAY as u64 {
            let result = service.execute(Request::Mkdir {
                id: id64(value),
                path: format!("dir/d{value}").into_bytes(),
                mode: 0o700,
            });
            assert!(result.is_ok(), "transaction {value}: {result:?}");
        }
        assert_eq!(service.execute(first).unwrap(), first_outcome);
        assert!(matches!(
            service.execute(Request::Mkdir {
                id: id64(MAX_REPLAY as u64 + 1),
                path: b"dir/overflow".to_vec(),
                mode: 0o700,
            }),
            Err(TransactionError::Protocol("transaction budget exhausted"))
        ));
        assert!(!roots.root.join("upper/dir/overflow").exists());
    }

    #[test]
    fn create_preserves_whitelisted_open_flags_and_rejects_others() {
        let roots = Roots::new();
        let mut service = roots.service();
        let outcome = service
            .execute(Request::Create {
                id: id(12),
                path: b"dir/append".to_vec(),
                flags: libc::O_WRONLY
                    | libc::O_APPEND
                    | libc::O_CREAT
                    | libc::O_EXCL
                    | libc::O_CLOEXEC,
                mode: 0o600,
            })
            .unwrap();
        let Outcome::Created { device, inode } = outcome else {
            panic!("create")
        };
        let fd = service
            .duplicate_created_result(id(12), device, inode)
            .unwrap();
        let status = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFL) };
        assert_eq!(status & libc::O_ACCMODE, libc::O_WRONLY);
        assert_ne!(status & libc::O_APPEND, 0);
        assert_ne!(
            unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETFD) } & libc::FD_CLOEXEC,
            0
        );
        assert!(matches!(
            service
                .execute(Request::Create {
                    id: id(13),
                    path: b"dir/truncate".to_vec(),
                    flags: libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_TRUNC
                        | libc::O_CLOEXEC,
                    mode: 0o600,
                })
                .unwrap(),
            Outcome::Rejected {
                errno: libc::ENOTSUP,
                ..
            }
        ));
    }

    #[test]
    fn leaf_replacement_before_unlink_is_quarantined_not_deleted() {
        let roots = Roots::new();
        let public = roots.root.join("upper/dir/victim");
        let saved = roots.root.join("upper/dir/original");
        fs::write(&public, b"original").unwrap();
        let hook_public = public.clone();
        let hook_saved = saved.clone();
        let mut service = roots.service();
        service.set_test_hook(move |checkpoint| {
            if checkpoint == TestCheckpoint::AfterLeafAuthority {
                fs::rename(&hook_public, &hook_saved).unwrap();
                fs::write(&hook_public, b"replacement").unwrap();
            }
        });
        assert!(matches!(
            service
                .execute(Request::Unlink {
                    id: id(14),
                    path: b"dir/victim".to_vec(),
                    flags: 0,
                })
                .unwrap(),
            Outcome::RecoveryRequired { .. }
        ));
        assert_eq!(fs::read(&saved).unwrap(), b"original");
        let RecoveryObligation::DisplacedSource { object, .. } = &service.recovery_obligations()[0]
        else {
            panic!("displaced source")
        };
        use std::os::unix::fs::MetadataExt;
        let saved_metadata = fs::metadata(&saved).unwrap();
        assert_eq!(
            identity(object.as_raw_fd()).unwrap().inode,
            saved_metadata.ino()
        );
        let RecoveryObligation::BoundQuarantine {
            staged_name,
            object: replacement,
            ..
        } = &service.recovery_obligations()[1]
        else {
            panic!("bound replacement")
        };
        assert_eq!(
            identity(replacement.as_raw_fd()).unwrap().inode,
            fs::metadata(
                roots
                    .root
                    .join("sidecar/quarantine")
                    .join(std::ffi::OsStr::from_bytes(staged_name))
            )
            .unwrap()
            .ino()
        );
        assert_eq!(
            fs::read(
                roots
                    .root
                    .join("sidecar/quarantine")
                    .join(std::ffi::OsStr::from_bytes(staged_name))
            )
            .unwrap(),
            b"replacement"
        );
    }

    #[test]
    fn leaf_replacement_before_rename_is_preserved_without_publication() {
        let roots = Roots::new();
        let source = roots.root.join("upper/dir/source-race");
        let saved = roots.root.join("upper/dir/source-original");
        fs::write(&source, b"original").unwrap();
        let hook_source = source.clone();
        let hook_saved = saved.clone();
        let mut service = roots.service();
        service.set_test_hook(move |checkpoint| {
            if checkpoint == TestCheckpoint::AfterLeafAuthority {
                fs::rename(&hook_source, &hook_saved).unwrap();
                fs::write(&hook_source, b"replacement").unwrap();
            }
        });
        assert!(matches!(
            service
                .execute(Request::Rename {
                    id: id(15),
                    source: b"dir/source-race".to_vec(),
                    destination: b"dir/destination-race".to_vec(),
                })
                .unwrap(),
            Outcome::RecoveryRequired { .. }
        ));
        assert_eq!(fs::read(saved).unwrap(), b"original");
        assert!(!roots.root.join("upper/dir/destination-race").exists());
        assert_eq!(service.recovery_obligations().len(), 2);
    }

    #[test]
    fn staged_replacement_before_rename_publication_is_detected() {
        let roots = Roots::new();
        let directory = roots.root.join("upper/dir");
        let quarantine = roots.root.join("sidecar/quarantine");
        fs::write(directory.join("source-stage-race"), b"original").unwrap();
        let hook_quarantine = quarantine.clone();
        let mut service = roots.service();
        service.set_test_hook(move |checkpoint| {
            if checkpoint == TestCheckpoint::AfterQuarantineVerify {
                let staged = fs::read_dir(&hook_quarantine)
                    .unwrap()
                    .map(|entry| entry.unwrap().path())
                    .find(|path| {
                        path.file_name()
                            .unwrap()
                            .as_bytes()
                            .starts_with(b".darling-txn-")
                    })
                    .unwrap();
                fs::rename(&staged, hook_quarantine.join("original-stage-race")).unwrap();
                fs::write(&staged, b"replacement").unwrap();
            }
        });
        assert!(matches!(
            service
                .execute(Request::Rename {
                    id: id(16),
                    source: b"dir/source-stage-race".to_vec(),
                    destination: b"dir/destination-stage-race".to_vec(),
                })
                .unwrap(),
            Outcome::RecoveryRequired {
                operation: "rename publication replacement"
            }
        ));
        assert_eq!(
            fs::read(quarantine.join("original-stage-race")).unwrap(),
            b"original"
        );
        assert_eq!(
            fs::read(directory.join("destination-stage-race")).unwrap(),
            b"replacement"
        );
        assert_eq!(service.recovery_obligations().len(), 2);
    }

    #[test]
    fn replacement_before_quiescent_gc_is_preserved_with_both_authorities() {
        let roots = Roots::new();
        let directory = roots.root.join("upper/dir");
        let quarantine = roots.root.join("sidecar/quarantine");
        fs::write(directory.join("gc-victim"), b"original").unwrap();
        let mut service = roots.service();
        assert_eq!(
            service
                .execute(Request::Unlink {
                    id: id(17),
                    path: b"dir/gc-victim".to_vec(),
                    flags: 0,
                })
                .unwrap(),
            Outcome::Mutated
        );
        let staged = fs::read_dir(&quarantine)
            .unwrap()
            .map(|entry| entry.unwrap().path())
            .find(|path| {
                path.file_name()
                    .unwrap()
                    .as_bytes()
                    .starts_with(b".darling-txn-")
            })
            .unwrap();
        let original = quarantine.join("gc-original");
        fs::rename(&staged, &original).unwrap();
        fs::write(&staged, b"replacement").unwrap();
        service.revoke();
        assert_eq!(fs::read(original).unwrap(), b"original");
        assert_eq!(fs::read(staged).unwrap(), b"replacement");
        assert_eq!(service.recovery_obligations().len(), 2);
    }

    #[test]
    fn crash_after_private_quarantine_move_recovers_exact_authority_from_wal() {
        let roots = Roots::new();
        let victim = roots.root.join("upper/dir/crash-victim");
        fs::write(&victim, b"durable-original").unwrap();
        let expected = fs::metadata(&victim).unwrap();
        use std::os::unix::fs::MetadataExt;

        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            let mut service = roots.service();
            service.set_test_hook(|checkpoint| {
                if checkpoint == TestCheckpoint::AfterQuarantineMove {
                    unsafe { libc::_exit(77) };
                }
            });
            let _ = service.execute(Request::Unlink {
                id: id(18),
                path: b"dir/crash-victim".to_vec(),
                flags: 0,
            });
            unsafe { libc::_exit(78) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert!(libc::WIFEXITED(status));
        assert_eq!(libc::WEXITSTATUS(status), 77);

        assert!(!victim.exists());
        let service = roots.service();
        assert!(!service.active);
        assert_eq!(service.recovery_obligations().len(), 1);
        let RecoveryObligation::BoundQuarantine {
            object,
            device,
            inode,
            ..
        } = &service.recovery_obligations()[0]
        else {
            panic!("restart must retain exact quarantined inode")
        };
        assert_eq!((*device, *inode), (expected.dev(), expected.ino()));
        assert_eq!(identity(object.as_raw_fd()).unwrap().inode, expected.ino());
        assert_eq!(
            fs::read(format!("/proc/self/fd/{}", object.as_raw_fd())).unwrap(),
            b"durable-original"
        );
    }

    #[test]
    fn effective_guest_mode_is_exact_despite_controller_umask() {
        let roots = Roots::new();
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::umask(0o777) };
            let mut service = roots.service();
            let outcome = service.execute(Request::Mkdir {
                id: id(19),
                path: b"dir/special-mode".to_vec(),
                mode: 0o3775,
            });
            let mode = fs::metadata(roots.root.join("upper/dir/special-mode"))
                .map(|metadata| metadata.permissions().mode() & 0o7777);
            unsafe {
                libc::_exit(i32::from(
                    !matches!(outcome, Ok(Outcome::Created { .. })) || mode.ok() != Some(0o3775),
                ))
            };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert!(libc::WIFEXITED(status));
        assert_eq!(libc::WEXITSTATUS(status), 0);
    }

    #[test]
    fn crash_after_rename_publication_recovers_destination_inode_from_wal() {
        let roots = Roots::new();
        let source = roots.root.join("upper/dir/restart-source");
        let destination = roots.root.join("upper/dir/restart-destination");
        fs::write(&source, b"published-before-crash").unwrap();
        let expected = fs::metadata(&source).unwrap();
        use std::os::unix::fs::MetadataExt;

        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            let mut service = roots.service();
            service.set_test_hook(|checkpoint| {
                if checkpoint == TestCheckpoint::AfterPublication {
                    unsafe { libc::_exit(79) };
                }
            });
            let _ = service.execute(Request::Rename {
                id: id(20),
                source: b"dir/restart-source".to_vec(),
                destination: b"dir/restart-destination".to_vec(),
            });
            unsafe { libc::_exit(80) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(libc::WEXITSTATUS(status), 79);
        assert!(!source.exists());
        assert_eq!(fs::read(&destination).unwrap(), b"published-before-crash");

        let service = roots.service();
        assert!(!service.active);
        let RecoveryObligation::BoundCreated {
            path,
            device,
            inode,
            object,
        } = &service.recovery_obligations()[0]
        else {
            panic!("restart must bind published destination")
        };
        assert_eq!(path, b"dir/restart-destination");
        assert_eq!((*device, *inode), (expected.dev(), expected.ino()));
        assert_eq!(identity(object.as_raw_fd()).unwrap().inode, expected.ino());
    }

    #[test]
    fn committed_unlink_restores_gc_pending_and_persists_gc_done() {
        let roots = Roots::new();
        fs::write(roots.root.join("upper/dir/gc-after-commit"), b"garbage").unwrap();
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            let mut service = roots.service();
            service.set_test_hook(|checkpoint| {
                if checkpoint == TestCheckpoint::AfterCommit {
                    unsafe { libc::_exit(81) };
                }
            });
            let _ = service.execute(Request::Unlink {
                id: id(21),
                path: b"dir/gc-after-commit".to_vec(),
                flags: 0,
            });
            unsafe { libc::_exit(82) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(libc::WEXITSTATUS(status), 81);

        let mut recovered = roots.service();
        assert!(recovered.active);
        assert_eq!(recovered.garbage.len(), 1);
        recovered.revoke();
        assert!(recovered.garbage.is_empty());
        assert!(recovered.recovery.is_empty());
        drop(recovered);

        let restarted = roots.service();
        assert!(restarted.garbage.is_empty());
        assert!(restarted.recovery.is_empty());
        assert_eq!(
            fs::read_dir(roots.root.join("sidecar/quarantine"))
                .unwrap()
                .count(),
            0
        );
    }

    #[test]
    fn committed_create_restart_replays_with_exact_retained_fd() {
        let roots = Roots::new();
        let request = Request::Create {
            id: id(22),
            path: b"dir/create-before-response".to_vec(),
            flags: libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            mode: 0o640,
        };
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            let mut service = roots.service();
            service.set_test_hook(|checkpoint| {
                if checkpoint == TestCheckpoint::AfterCommit {
                    unsafe { libc::_exit(83) };
                }
            });
            let _ = service.execute(request.clone());
            unsafe { libc::_exit(84) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(libc::WEXITSTATUS(status), 83);

        let mut recovered = roots.service();
        let Outcome::Created { device, inode } = recovered.execute(request).unwrap() else {
            panic!("committed create replay")
        };
        let fd = recovered
            .duplicate_created_result(id(22), device, inode)
            .unwrap();
        assert_eq!(
            identity(fd.as_raw_fd()).unwrap(),
            Identity { device, inode }
        );
    }

    #[test]
    fn torn_tail_is_physically_repaired_before_new_commit_and_second_restart() {
        use std::io::Write as _;
        let roots = Roots::new();
        let first = Request::Create {
            id: id(23),
            path: b"dir/torn-first".to_vec(),
            flags: libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            mode: 0o600,
        };
        let mut initial = roots.service();
        let first_outcome = initial.execute(first.clone()).unwrap();
        drop(initial);
        let journal_path = roots.root.join("sidecar/transactions.wal");
        let clean_size = fs::metadata(&journal_path).unwrap().len();
        let mut journal = fs::OpenOptions::new()
            .append(true)
            .open(&journal_path)
            .unwrap();
        journal.write_all(b"B torn-partial-tail").unwrap();
        journal.sync_all().unwrap();
        drop(journal);

        let mut recovered = roots.service();
        assert_eq!(fs::metadata(&journal_path).unwrap().len(), clean_size);
        assert_eq!(recovered.execute(first.clone()).unwrap(), first_outcome);
        let second = Request::Mkdir {
            id: id(24),
            path: b"dir/after-tail-repair".to_vec(),
            mode: 0o750,
        };
        let second_outcome = recovered.execute(second.clone()).unwrap();
        drop(recovered);

        let mut second_restart = roots.service();
        assert_eq!(second_restart.execute(first).unwrap(), first_outcome);
        assert_eq!(second_restart.execute(second).unwrap(), second_outcome);
        assert!(second_restart.recovery.is_empty());
    }

    #[test]
    fn ambiguous_wal_capacity_failure_closes_admission_before_mutation() {
        let roots = Roots::new();
        let mut service = roots.service();
        assert_eq!(
            unsafe { libc::ftruncate(service.journal.as_raw_fd(), MAX_JOURNAL_BYTES as _) },
            0
        );
        assert!(matches!(
            service.execute(Request::Mkdir {
                id: id(25),
                path: b"dir/wal-full".to_vec(),
                mode: 0o700,
            }),
            Err(TransactionError::Protocol("journal byte budget exhausted"))
        ));
        assert!(!service.active);
        assert!(!roots.root.join("upper/dir/wal-full").exists());
    }

    #[test]
    fn create_commit_write_and_fsync_failures_retain_post_mutation_authority() {
        for (index, fault) in [JournalFault::CommitWrite, JournalFault::CommitFsync]
            .into_iter()
            .enumerate()
        {
            let roots = Roots::new();
            let transaction = id(30 + index as u8);
            let path = format!("dir/create-commit-fault-{index}").into_bytes();
            let request = Request::Create {
                id: transaction,
                path: path.clone(),
                flags: libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                mode: 0o640,
            };
            let mut service = roots.service();
            service.set_journal_fault(fault);
            assert!(service.execute(request.clone()).is_err());
            assert!(!service.active);
            assert!(matches!(
                service.recovery.as_slice(),
                [RecoveryObligation::BoundCreated { path: retained, .. }] if retained == &path
            ));
            assert!(service.prepare_clean_sidecar().is_err());
            drop(service);
            let mut restarted = roots.service();
            assert!(
                restarted.has_outstanding_recovery()
                    || matches!(restarted.execute(request), Ok(Outcome::Created { .. }))
            );
        }
    }

    #[test]
    fn unlink_commit_write_and_fsync_failures_retain_quarantine_authority() {
        for (index, fault) in [JournalFault::CommitWrite, JournalFault::CommitFsync]
            .into_iter()
            .enumerate()
        {
            let roots = Roots::new();
            let victim = roots
                .root
                .join(format!("upper/dir/unlink-commit-fault-{index}"));
            fs::write(&victim, b"unlink-authority").unwrap();
            let transaction = id(32 + index as u8);
            let request = Request::Unlink {
                id: transaction,
                path: format!("dir/unlink-commit-fault-{index}").into_bytes(),
                flags: 0,
            };
            let mut service = roots.service();
            service.set_journal_fault(fault);
            assert!(service.execute(request.clone()).is_err());
            assert!(!victim.exists());
            assert!(matches!(
                service.recovery.as_slice(),
                [RecoveryObligation::BoundQuarantine { object, .. }]
                    if fs::read(format!("/proc/self/fd/{}", object.as_raw_fd())).unwrap()
                        == b"unlink-authority"
            ));
            assert!(service.prepare_clean_sidecar().is_err());
            drop(service);
            let mut restarted = roots.service();
            assert!(
                restarted.has_outstanding_recovery()
                    || matches!(restarted.execute(request), Ok(Outcome::Mutated))
            );
        }
    }

    #[test]
    fn rename_commit_write_and_fsync_failures_retain_published_authority() {
        for (index, fault) in [JournalFault::CommitWrite, JournalFault::CommitFsync]
            .into_iter()
            .enumerate()
        {
            let roots = Roots::new();
            let source = roots.root.join(format!("upper/dir/rename-source-{index}"));
            let destination = roots
                .root
                .join(format!("upper/dir/rename-destination-{index}"));
            fs::write(&source, b"rename-authority").unwrap();
            let transaction = id(34 + index as u8);
            let request = Request::Rename {
                id: transaction,
                source: format!("dir/rename-source-{index}").into_bytes(),
                destination: format!("dir/rename-destination-{index}").into_bytes(),
            };
            let mut service = roots.service();
            service.set_journal_fault(fault);
            assert!(service.execute(request.clone()).is_err());
            assert!(!source.exists());
            assert_eq!(fs::read(&destination).unwrap(), b"rename-authority");
            assert!(matches!(
                service.recovery.as_slice(),
                [RecoveryObligation::BoundCreated { object, .. }]
                    if fs::read(format!("/proc/self/fd/{}", object.as_raw_fd())).unwrap()
                        == b"rename-authority"
            ));
            assert!(service.prepare_clean_sidecar().is_err());
            drop(service);
            let mut restarted = roots.service();
            assert!(
                restarted.has_outstanding_recovery()
                    || matches!(restarted.execute(request), Ok(Outcome::Mutated))
            );
        }
    }
}
