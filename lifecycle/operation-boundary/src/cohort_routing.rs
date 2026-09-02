//! Rust-owned routing boundary for the first Rootless namespace-writer cohort.
//!
//! The controller retains the prefix directory and the exact
//! `.lifecycle.lock` open-file description for its whole lifetime.  Product
//! processes never receive pathname mutation authority: they request one of
//! the finite endpoint kinds and receive only the already-bound listener via
//! `SCM_RIGHTS`.  The v1 threat model is deliberately cooperative; writers
//! outside this cohort keep global routing disabled.

#![deny(unsafe_code)]

use crate::guest_namespace_authority::GuestNamespaceAuthority;
use crate::guest_namespace_transaction::{
    GuestNamespaceTransactionService, Outcome as GuestTransactionOutcome,
    Request as GuestTransactionRequest, TransactionId,
};
use crate::runtime_lower_binding::RuntimeLowerBinding;
use crate::FileIdentity;
use std::collections::BTreeMap;
use std::ffi::{CStr, CString};
use std::io;
use std::mem::{size_of, MaybeUninit};
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::os::raw::{c_char, c_int};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::ptr;
use std::sync::Mutex;
use std::thread;
#[cfg(test)]
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

#[path = "cohort_ffi.rs"]
#[allow(unsafe_code)]
mod cohort_ffi;
pub use cohort_ffi::*;

const PREFIX_STATE_V2_NAME: &[u8] = b".darling-prefix-state-v2";
const PREFIX_STATE_V2_HEADER: &str = "DARLING_PREFIX_STATE_V2";
const PREFIX_STATE_V2_PROVENANCE: &str = "darling-runtime-prefix-lifecycle-v2";
const PREFIX_STATE_V3_NAME: &[u8] = b".darling-prefix-state-v3";
const PREFIX_STATE_V3_HEADER: &str = "DARLING_PREFIX_STATE_V3";
const PREFIX_STATE_V3_PROVENANCE: &str = "darling-runtime-prefix-sidecar-v1";
const PREFIX_STATE_MAX_BYTES: usize = 1024;
const LOCK_NAME: &[u8] = b".lifecycle.lock";
const INIT_PID_NAME: &[u8] = b".init.pid";
const PROTOCOL_MAGIC: [u8; 8] = *b"DLCOHR1\0";
const PROTOCOL_VERSION: u16 = 2;
const OPERATION_PUBLISH: u16 = 1;
const OPERATION_RETIRE: u16 = 2;
const OPERATION_COMMIT: u16 = 3;
const OPERATION_ABORT: u16 = 4;
const RESPONSE_PHASE_REQUEST: u16 = 1;
const RESPONSE_PHASE_PUBLISH: u16 = 2;
const RESPONSE_PHASE_COMMIT: u16 = 3;
const RESPONSE_PHASE_ABORT: u16 = 4;
const RESPONSE_PHASE_RETIRE: u16 = 5;
const RESPONSE_ERROR_NONE: i32 = 0;
const RESPONSE_ERROR_IO: i32 = 1;
const RESPONSE_ERROR_IDENTITY: i32 = 2;
const RESPONSE_ERROR_LOCK_BUSY: i32 = 3;
const RESPONSE_ERROR_PROTOCOL: i32 = 4;
const RESPONSE_ERROR_ENDPOINT_EXISTS: i32 = 5;
const RESPONSE_ERROR_ENDPOINT_MISSING: i32 = 6;
const RESPONSE_ERROR_PROCESS: i32 = 7;
const CONTROL_NAME_CAPACITY: usize = 80;
const NONCE_BYTES: usize = 32;
const NONCE_HEX_BYTES: usize = NONCE_BYTES * 2;
const DYNAMIC_PATH_CAPACITY: usize = 96;
const DYNAMIC_DIRECTORY_ATTEMPTS: usize = 16;
const LOCK_TIMEOUT: Duration = Duration::from_millis(250);
const REQUEST_TIMEOUT_MS: c_int = 250;
const ABANDON_PENDING_STATUS: c_int = 3;
const CLEANUP_PENDING_STATUS: c_int = 4;
const RECOVERY_PENDING_STATUS: c_int = 5;
const CONTROLLER_COMMAND_CLEANUP: u8 = 1;
const CONTROLLER_COMMAND_PRESERVE: u8 = 2;
const CONTROLLER_COMMAND_ACK: u8 = 0x7f;
const CONTROLLER_EXIT_TIMEOUT_MS: c_int = 1_000;
const MAX_REJECTED_REQUESTS_PER_SLICE: usize = 128;
const SO_PEERPIDFD: c_int = 77;
#[cfg_attr(test, allow(dead_code))]
const CONTROLLER_WORKER_PROTOCOL: u32 = 1;
#[cfg_attr(test, allow(dead_code))]
const CONTROLLER_WORKER_FD_COUNT: usize = 12;
// Keep the vchroot-visible name short: the host prefix is prepended before
// connect(2), and AF_UNIX sun_path is only 108 bytes on Linux.
const CONTROL_GUEST_PATH: &[u8] = b"/.lc-v1.sock";
const CONTROL_NAME: &[u8] = b".lc-v1.sock";
const SHELLSPAWN_PARENT: &[&[u8]] = &[b"var", b"run"];
const LAUNCHD_PARENT: &[&[u8]] = &[b"var", b"tmp", b"launchd"];
const PER_USER_PARENT: &[&[u8]] = &[b"private", b"var", b"tmp"];
const DSERVER_LOG_PARENT: &[&[u8]] = &[b"private", b"var", b"log"];
const DSERVER_LOG_NAME: &[u8] = b"dserver.log";

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
#[repr(u16)]
pub enum CohortEndpoint {
    DarlingServer = 1,
    Shellspawn = 2,
    Launchd = 3,
    #[doc(hidden)]
    Control = 4,
    PerUserLaunchd = 5,
}

impl CohortEndpoint {
    fn from_wire(value: u16) -> Option<Self> {
        match value {
            1 => Some(Self::DarlingServer),
            2 => Some(Self::Shellspawn),
            3 => Some(Self::Launchd),
            5 => Some(Self::PerUserLaunchd),
            _ => None,
        }
    }

    fn spec(self) -> Option<EndpointSpec> {
        match self {
            Self::DarlingServer => Some(EndpointSpec {
                parent: &[],
                name: b".darlingserver.sock",
                socket_type: libc::SOCK_DGRAM,
                nonblocking: true,
                listen_backlog: None,
                mode: 0o775,
            }),
            Self::Shellspawn => Some(EndpointSpec {
                parent: SHELLSPAWN_PARENT,
                name: b"shellspawn.sock",
                socket_type: libc::SOCK_STREAM,
                nonblocking: false,
                listen_backlog: Some(16_384),
                mode: 0o600,
            }),
            Self::Launchd => Some(EndpointSpec {
                parent: LAUNCHD_PARENT,
                name: b"sock",
                socket_type: libc::SOCK_STREAM,
                nonblocking: false,
                listen_backlog: Some(libc::SOMAXCONN),
                mode: 0o600,
            }),
            Self::Control => Some(EndpointSpec {
                parent: &[],
                name: CONTROL_NAME,
                socket_type: libc::SOCK_SEQPACKET,
                nonblocking: true,
                listen_backlog: Some(16),
                mode: 0o600,
            }),
            Self::PerUserLaunchd => None,
        }
    }
}

#[derive(Clone, Copy)]
struct EndpointSpec {
    parent: &'static [&'static [u8]],
    name: &'static [u8],
    socket_type: c_int,
    nonblocking: bool,
    listen_backlog: Option<c_int>,
    mode: u32,
}

#[derive(Debug)]
pub enum CohortError {
    Io(&'static str, io::Error),
    InvalidPrefix,
    Identity(&'static str),
    LockBusy,
    Protocol(&'static str),
    EndpointExists,
    EndpointMissing,
    Thread,
    Process,
}

impl std::fmt::Display for CohortError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(operation, error) => write!(formatter, "{operation}: {error}"),
            Self::InvalidPrefix => write!(formatter, "invalid prefix"),
            Self::Identity(object) => write!(formatter, "identity mismatch: {object}"),
            Self::LockBusy => write!(formatter, "lifecycle lease unavailable"),
            Self::Protocol(reason) => write!(formatter, "cohort protocol: {reason}"),
            Self::EndpointExists => write!(formatter, "endpoint already published"),
            Self::EndpointMissing => write!(formatter, "endpoint is not published"),
            Self::Thread => write!(formatter, "controller thread failed"),
            Self::Process => write!(formatter, "controller process failed"),
        }
    }
}

impl std::error::Error for CohortError {}

fn io_error(operation: &'static str) -> CohortError {
    CohortError::Io(operation, io::Error::last_os_error())
}

fn component(bytes: &[u8]) -> Result<CString, CohortError> {
    if bytes.is_empty() || bytes == b"." || bytes == b".." || bytes.contains(&b'/') {
        return Err(CohortError::Protocol("invalid component"));
    }
    CString::new(bytes).map_err(|_| CohortError::Protocol("component contains NUL"))
}

fn duplicate(fd: BorrowedFd<'_>) -> Result<OwnedFd, CohortError> {
    rustix::io::fcntl_dupfd_cloexec(fd, 3)
        .map_err(|error| CohortError::Io("fcntl(F_DUPFD_CLOEXEC)", error.into()))
}

fn openat(
    parent: BorrowedFd<'_>,
    name: &[u8],
    flags: c_int,
    mode: libc::mode_t,
) -> Result<OwnedFd, CohortError> {
    let name = component(name)?;
    rustix::fs::openat(
        parent,
        name.as_c_str(),
        rustix::fs::OFlags::from_bits_retain(flags as u32),
        rustix::fs::Mode::from_raw_mode(mode),
    )
    .map_err(|error| CohortError::Io("openat", error.into()))
}

fn identity(fd: RawFd) -> Result<FileIdentity, CohortError> {
    FileIdentity::from_fd(fd).map_err(|_| io_error("fstat"))
}

fn named_identity(
    parent: BorrowedFd<'_>,
    name: &[u8],
) -> Result<Option<FileIdentity>, CohortError> {
    let name = component(name)?;
    match FileIdentity::from_at(parent.as_raw_fd(), &name) {
        Ok(value) => Ok(Some(value)),
        Err(_) if io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) => Ok(None),
        Err(_) => Err(io_error("fstatat")),
    }
}

fn file_type(value: FileIdentity) -> u32 {
    value.mode & libc::S_IFMT
}

fn same_retained_object(left: FileIdentity, right: FileIdentity) -> bool {
    left.device == right.device
        && left.inode == right.inode
        && left.mode == right.mode
        && left.uid == right.uid
        && left.gid == right.gid
}

fn current_uid() -> libc::uid_t {
    rustix::process::geteuid().as_raw()
}

fn current_gid() -> libc::gid_t {
    rustix::process::getegid().as_raw()
}

fn validate_owned(
    value: FileIdentity,
    expected_type: u32,
    mode: Option<u32>,
) -> Result<(), CohortError> {
    if file_type(value) != expected_type
        || value.nlink != 1
        || value.uid != current_uid()
        || mode.is_some_and(|expected| value.mode & 0o777 != expected)
    {
        return Err(CohortError::Identity("owned object"));
    }
    Ok(())
}

fn read_bounded(fd: BorrowedFd<'_>, limit: usize) -> Result<Vec<u8>, CohortError> {
    rustix::fs::seek(fd, rustix::fs::SeekFrom::Start(0))
        .map_err(|error| CohortError::Io("lseek", error.into()))?;
    let mut output = Vec::with_capacity(limit.min(4096));
    let mut buffer = [0u8; 256];
    loop {
        let count = rustix::io::read(fd, &mut buffer)
            .map_err(|error| CohortError::Io("read", error.into()))?;
        if count == 0 {
            return Ok(output);
        }
        if output.len() + count > limit {
            return Err(CohortError::Protocol("bounded file exceeded"));
        }
        output.extend_from_slice(&buffer[..count]);
    }
}

fn write_all(fd: BorrowedFd<'_>, mut bytes: &[u8]) -> Result<(), CohortError> {
    while !bytes.is_empty() {
        let count = match rustix::io::write(fd, bytes) {
            Ok(count) => count,
            Err(rustix::io::Errno::INTR) => {
                continue;
            }
            Err(error) => return Err(CohortError::Io("write", error.into())),
        };
        if count == 0 {
            return Err(CohortError::Protocol("zero-length write"));
        }
        bytes = &bytes[count..];
    }
    Ok(())
}

fn open_prefix(path: &Path) -> Result<OwnedFd, CohortError> {
    if !path.is_absolute() {
        return Err(CohortError::InvalidPrefix);
    }
    let fd = rustix::fs::open(
        path,
        rustix::fs::OFlags::PATH
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|error| CohortError::Io("open(prefix)", error.into()))?;
    let observed = identity(fd.as_raw_fd())?;
    if file_type(observed) != libc::S_IFDIR || observed.uid != current_uid() {
        return Err(CohortError::Identity("prefix"));
    }
    Ok(fd)
}

fn open_directory_chain(prefix: BorrowedFd<'_>, parts: &[&[u8]]) -> Result<OwnedFd, CohortError> {
    let mut current = duplicate(prefix)?;
    for part in parts {
        current = openat(
            current.as_fd(),
            part,
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0,
        )?;
        let observed = identity(current.as_raw_fd())?;
        if file_type(observed) != libc::S_IFDIR || observed.uid != current_uid() {
            return Err(CohortError::Identity("endpoint parent"));
        }
    }
    Ok(current)
}

fn ensure_directory_chain(
    prefix: BorrowedFd<'_>,
    parts: &[(&[u8], u32)],
) -> Result<OwnedFd, CohortError> {
    let mut current = duplicate(prefix)?;
    for (part, mode) in parts {
        let next = match openat(
            current.as_fd(),
            part,
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0,
        ) {
            Ok(value) => value,
            Err(CohortError::Io(_, error)) if error.raw_os_error() == Some(libc::ENOENT) => {
                let name = component(part)?;
                rustix::fs::mkdirat(
                    current.as_fd(),
                    name.as_c_str(),
                    rustix::fs::Mode::from_raw_mode(*mode),
                )
                .map_err(|error| CohortError::Io("mkdirat(endpoint parent)", error.into()))?;
                openat(
                    current.as_fd(),
                    part,
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0,
                )?
            }
            Err(error) => return Err(error),
        };
        let observed = identity(next.as_raw_fd())?;
        if file_type(observed) != libc::S_IFDIR || observed.uid != current_uid() {
            return Err(CohortError::Identity("endpoint parent"));
        }
        if identity(next.as_raw_fd())?.mode & 0o7777 != *mode {
            // SessionAuthority already owns the exact exclusive lifecycle
            // lease. Normalize both newly created parents and retained legacy
            // bootstrap directories through the retained descriptor.
            rustix::fs::fchmod(next.as_fd(), rustix::fs::Mode::from_raw_mode(*mode))
                .map_err(|error| CohortError::Io("fchmod(endpoint parent)", error.into()))?;
            if identity(next.as_raw_fd())?.mode & 0o7777 != *mode {
                return Err(CohortError::Identity("endpoint parent mode"));
            }
        }
        current = next;
    }
    Ok(current)
}

fn ensure_endpoint_parents(prefix: BorrowedFd<'_>) -> Result<(), CohortError> {
    ensure_directory_chain(prefix, &[(b"var", 0o755), (b"run", 0o755)])?;
    ensure_directory_chain(
        prefix,
        &[(b"var", 0o755), (b"tmp", 0o1777), (b"launchd", 0o700)],
    )?;
    ensure_directory_chain(
        prefix,
        &[(b"private", 0o755), (b"var", 0o755), (b"tmp", 0o1777)],
    )?;
    ensure_directory_chain(
        prefix,
        &[(b"private", 0o755), (b"var", 0o755), (b"log", 0o755)],
    )?;
    Ok(())
}

fn rollback_created_log(
    parent: BorrowedFd<'_>,
    name: &[u8],
    expected: FileIdentity,
) -> Result<(), CohortError> {
    if named_identity(parent, name)?.map(FileIdentity::inode_key) != Some(expected.inode_key()) {
        return Err(CohortError::Identity("new Darlingserver log rollback"));
    }
    let name = component(name)?;
    rustix::fs::unlinkat(parent, name.as_c_str(), rustix::fs::AtFlags::empty()).map_err(
        |error| CohortError::Io("unlinkat(new Darlingserver log rollback)", error.into()),
    )?;
    if named_identity(parent, name.as_bytes())?.is_some() {
        return Err(CohortError::Identity(
            "new Darlingserver log rollback result",
        ));
    }
    Ok(())
}

fn acquire_lock(prefix: BorrowedFd<'_>) -> Result<(OwnedFd, FileIdentity), CohortError> {
    let (lock, created) = match openat(
        prefix,
        LOCK_NAME,
        libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0,
    ) {
        Ok(value) => (value, false),
        Err(CohortError::Io(_, error)) if error.raw_os_error() == Some(libc::ENOENT) => (
            openat(
                prefix,
                LOCK_NAME,
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0o600,
            )?,
            true,
        ),
        Err(error) => return Err(error),
    };
    let mut expected = identity(lock.as_raw_fd())?;
    validate_owned(expected, libc::S_IFREG, (!created).then_some(0o600))?;
    let deadline = Instant::now() + LOCK_TIMEOUT;
    loop {
        match rustix::fs::flock(
            lock.as_fd(),
            rustix::fs::FlockOperation::NonBlockingLockExclusive,
        ) {
            Ok(()) => break,
            Err(rustix::io::Errno::AGAIN) => {}
            Err(error) => return Err(CohortError::Io("flock(LOCK_EX)", error.into())),
        }
        if Instant::now() >= deadline {
            return Err(CohortError::LockBusy);
        }
        thread::sleep(Duration::from_millis(2));
    }
    if named_identity(prefix, LOCK_NAME)? != Some(expected) {
        return Err(CohortError::Identity("split lifecycle lock"));
    }
    // An existing lock is immutable until its exact lease is held.  Only the
    // inode created by this acquisition may need mode normalization after the
    // lease transition (for example under an aggressive umask).
    if created {
        rustix::fs::fchmod(lock.as_fd(), rustix::fs::Mode::from_raw_mode(0o600))
            .map_err(|error| CohortError::Io("fchmod(new lock)", error.into()))?;
    }
    expected = identity(lock.as_raw_fd())?;
    validate_owned(expected, libc::S_IFREG, Some(0o600))?;
    if named_identity(prefix, LOCK_NAME)? != Some(expected) {
        return Err(CohortError::Identity("split lifecycle lock"));
    }
    Ok((lock, expected))
}

fn parse_prefix_state(content: &[u8], prefix: FileIdentity) -> Result<u64, CohortError> {
    let text = std::str::from_utf8(content)
        .map_err(|_| CohortError::Protocol("runtime prefix state encoding"))?;
    let body = text
        .strip_suffix('\n')
        .ok_or(CohortError::Protocol("runtime prefix state terminator"))?;
    let lines = body.split('\n').collect::<Vec<_>>();
    let field = |index: usize, key: &'static str| -> Result<&str, CohortError> {
        lines[index]
            .strip_prefix(key)
            .filter(|value| !value.is_empty())
            .ok_or(CohortError::Protocol("runtime prefix state field"))
    };
    let number = |index: usize, key: &'static str| -> Result<u64, CohortError> {
        field(index, key)?
            .parse::<u64>()
            .map_err(|_| CohortError::Protocol("runtime prefix state number"))
    };
    let generation = match (lines.len(), lines.first().copied()) {
        (9, Some(PREFIX_STATE_V2_HEADER)) => {
            if number(1, "schema_version=")? != 2
                || field(2, "runtime_mode=")? != "rootless-eunion"
                || number(3, "generation=")? == 0
                || number(4, "prefix_device=")? != prefix.device
                || number(5, "prefix_inode=")? != prefix.inode
                || number(6, "owner_uid=")? != u64::from(current_uid())
                || number(7, "owner_gid=")? != u64::from(current_gid())
                || field(8, "provenance=")? != PREFIX_STATE_V2_PROVENANCE
            {
                return Err(CohortError::Identity("runtime prefix state"));
            }
            number(3, "generation=")?
        }
        (11, Some(PREFIX_STATE_V3_HEADER)) => {
            if number(1, "schema_version=")? != 3
                || field(2, "runtime_mode=")? != "rootless-eunion"
                || number(3, "generation=")? == 0
                || number(4, "prefix_device=")? != prefix.device
                || number(5, "prefix_inode=")? != prefix.inode
                || number(6, "sidecar_device=")? == 0
                || number(7, "sidecar_inode=")? == 0
                || number(8, "owner_uid=")? != u64::from(current_uid())
                || number(9, "owner_gid=")? != u64::from(current_gid())
                || field(10, "provenance=")? != PREFIX_STATE_V3_PROVENANCE
            {
                return Err(CohortError::Identity("runtime prefix state"));
            }
            number(3, "generation=")?
        }
        _ => return Err(CohortError::Protocol("runtime prefix state schema")),
    };
    Ok(generation)
}

fn acquire_prefix_state(
    prefix: BorrowedFd<'_>,
    prefix_identity: FileIdentity,
) -> Result<RetainedState, CohortError> {
    let name = if named_identity(prefix, PREFIX_STATE_V3_NAME)?.is_some() {
        PREFIX_STATE_V3_NAME
    } else if named_identity(prefix, PREFIX_STATE_V2_NAME)?.is_some() {
        PREFIX_STATE_V2_NAME
    } else {
        return Err(CohortError::Identity("runtime prefix state"));
    };
    let state = openat(
        prefix,
        name,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0,
    )?;
    let expected = identity(state.as_raw_fd())?;
    validate_owned(expected, libc::S_IFREG, Some(0o600))?;
    let content = read_bounded(state.as_fd(), PREFIX_STATE_MAX_BYTES)?;
    if named_identity(prefix, name)? != Some(expected) {
        return Err(CohortError::Identity("runtime prefix state"));
    }
    let generation = parse_prefix_state(&content, prefix_identity)?;
    Ok(RetainedState {
        object: state,
        identity: expected,
        content,
        name: name.to_vec(),
        generation,
    })
}

fn revalidate_prefix_state(
    prefix: BorrowedFd<'_>,
    prefix_identity: FileIdentity,
    state: &RetainedState,
) -> Result<(), CohortError> {
    let content = read_bounded(state.object.as_fd(), PREFIX_STATE_MAX_BYTES)?;
    if identity(state.object.as_raw_fd())? != state.identity
        || named_identity(prefix, &state.name)? != Some(state.identity)
        || content != state.content
    {
        return Err(CohortError::Identity("runtime prefix state"));
    }
    parse_prefix_state(&content, prefix_identity).map(|_| ())
}

fn revalidate_lock(
    prefix: BorrowedFd<'_>,
    lock: BorrowedFd<'_>,
    expected: FileIdentity,
) -> Result<(), CohortError> {
    if identity(lock.as_raw_fd())? != expected
        || named_identity(prefix, LOCK_NAME)? != Some(expected)
    {
        return Err(CohortError::Identity("split lifecycle lock"));
    }
    Ok(())
}

fn random_nonce() -> Result<[u8; NONCE_BYTES], CohortError> {
    let mut nonce = [0u8; NONCE_BYTES];
    rustix::rand::getrandom(&mut nonce, rustix::rand::GetRandomFlags::empty())
        .map_err(|error| CohortError::Io("getrandom", error.into()))?;
    Ok(nonce)
}

fn nonce_hex(nonce: &[u8; NONCE_BYTES]) -> [u8; NONCE_HEX_BYTES] {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = [0u8; NONCE_HEX_BYTES];
    for (index, byte) in nonce.iter().copied().enumerate() {
        output[index * 2] = HEX[(byte >> 4) as usize];
        output[index * 2 + 1] = HEX[(byte & 0xf) as usize];
    }
    output
}

#[derive(Debug)]
struct PublishedEndpoint {
    parent: OwnedFd,
    parent_identity: FileIdentity,
    object: OwnedFd,
    identity: FileIdentity,
    name: Vec<u8>,
    dynamic_directory: Option<RetainedDirectory>,
    endpoint_linked: bool,
}

#[derive(Debug)]
struct RetainedDirectory {
    parent: OwnedFd,
    parent_identity: FileIdentity,
    identity: FileIdentity,
    name: Vec<u8>,
}

#[derive(Debug)]
struct RetainedFile {
    object: OwnedFd,
    identity: FileIdentity,
}

#[derive(Debug)]
struct PublishedLog {
    parent: OwnedFd,
    parent_identity: FileIdentity,
    object: OwnedFd,
    identity: FileIdentity,
    name: Vec<u8>,
}

#[derive(Debug)]
struct RetainedState {
    object: OwnedFd,
    identity: FileIdentity,
    content: Vec<u8>,
    name: Vec<u8>,
    generation: u64,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct PeerIdentity {
    pid: libc::pid_t,
    starttime: u64,
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
enum EndpointKey {
    Static(CohortEndpoint),
    PerUser(PeerIdentity),
}

#[derive(Debug)]
struct PeerAuthority {
    identity: PeerIdentity,
    _process: OwnedFd,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum OwnerDeathTransition {
    Fresh,
    RetainedAlive,
    RetiredGone,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum OwnerProcessState {
    Alive,
    Gone,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum DynamicPublicationFault {
    AfterDirectoryCreate,
}

fn owner_process_state(pidfd: RawFd) -> Result<OwnerProcessState, CohortError> {
    match cohort_ffi::probe_pidfd(pidfd) {
        Ok(()) => Ok(OwnerProcessState::Alive),
        Err(error) if error.raw_os_error() == Some(libc::ESRCH) => Ok(OwnerProcessState::Gone),
        Err(error) => Err(CohortError::Io("pidfd_send_signal(owner probe)", error)),
    }
}

fn peer_pidfd(socket: RawFd, expected_pid: libc::pid_t) -> Result<OwnedFd, CohortError> {
    let pidfd = cohort_ffi::peer_pidfd_from_socket(socket)?;
    let path = format!("/proc/self/fdinfo/{}", pidfd.as_raw_fd());
    let fd = rustix::fs::open(
        &path,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|error| {
        CohortError::Io(
            "open(peer pidfd info)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    let info = read_bounded(fd.as_fd(), 4096)?;
    let observed_pid = info
        .split(|byte| *byte == b'\n')
        .find_map(|line| {
            let value = line.strip_prefix(b"Pid:")?;
            std::str::from_utf8(value)
                .ok()?
                .trim()
                .parse::<libc::pid_t>()
                .ok()
        })
        .ok_or(CohortError::Protocol("peer pidfd identity"))?;
    if observed_pid != expected_pid
        || owner_process_state(pidfd.as_raw_fd())? != OwnerProcessState::Alive
    {
        return Err(CohortError::Protocol("peer pidfd mismatch"));
    }
    Ok(pidfd)
}

fn process_identity(pid: libc::pid_t) -> Result<(PeerIdentity, libc::pid_t), CohortError> {
    if pid <= 0 {
        return Err(CohortError::Protocol("peer pid"));
    }
    let path = format!("/proc/{pid}/stat");
    let fd = rustix::fs::open(
        &path,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|error| {
        CohortError::Io(
            "open(proc stat)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    let bytes = read_bounded(fd.as_fd(), 4096)?;
    let close = bytes
        .iter()
        .rposition(|byte| *byte == b')')
        .ok_or(CohortError::Protocol("proc stat comm"))?;
    let fields = bytes
        .get(close + 2..)
        .ok_or(CohortError::Protocol("proc stat fields"))?
        .split(|byte| byte.is_ascii_whitespace())
        .filter(|field| !field.is_empty())
        .collect::<Vec<_>>();
    if fields.len() <= 19 {
        return Err(CohortError::Protocol("proc stat truncated"));
    }
    let parse = |field: &[u8]| -> Result<u64, CohortError> {
        std::str::from_utf8(field)
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .ok_or(CohortError::Protocol("proc stat integer"))
    };
    let parent = parse(fields[1])? as libc::pid_t;
    let starttime = parse(fields[19])?;
    Ok((PeerIdentity { pid, starttime }, parent))
}

fn process_argv(pid: libc::pid_t) -> Result<Vec<Vec<u8>>, CohortError> {
    let path = format!("/proc/{pid}/cmdline");
    let fd = rustix::fs::open(
        &path,
        rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|error| {
        CohortError::Io(
            "open(proc cmdline)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    let bytes = read_bounded(fd.as_fd(), 4096)?;
    let argv = bytes
        .split(|byte| *byte == 0)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
        .collect::<Vec<_>>();
    if argv.is_empty() || argv.len() > 16 {
        return Err(CohortError::Protocol("peer argv budget"));
    }
    Ok(argv)
}

fn argv0_basename(argv: &[Vec<u8>]) -> &[u8] {
    argv[0]
        .rsplit(|byte| *byte == b'/')
        .next()
        .unwrap_or(&argv[0])
}

struct SessionAuthority {
    prefix: OwnedFd,
    prefix_identity: FileIdentity,
    lock: OwnedFd,
    lock_identity: FileIdentity,
    prefix_state: RetainedState,
    init_pid: Option<RetainedFile>,
    endpoints: BTreeMap<EndpointKey, PublishedEndpoint>,
    endpoint_owners: BTreeMap<EndpointKey, PeerAuthority>,
    log: Option<PublishedLog>,
    session_root_pid: libc::pid_t,
    #[cfg(test)]
    prefix_argument: Vec<u8>,
    cleanup_on_drop: bool,
    #[cfg(test)]
    allow_test_peer: bool,
    #[cfg(test)]
    dynamic_fault: Option<DynamicPublicationFault>,
    #[cfg(test)]
    fail_log_after_create: bool,
}

impl SessionAuthority {
    fn acquire(prefix_path: &Path, init_pid: libc::pid_t) -> Result<Self, CohortError> {
        let prefix = open_prefix(prefix_path)?;
        Self::acquire_owned(
            prefix,
            prefix_path.as_os_str().as_bytes().to_vec(),
            init_pid,
        )
    }

    fn acquire_from_fd(
        prefix_fd: RawFd,
        prefix_argument: &[u8],
        init_pid: libc::pid_t,
    ) -> Result<Self, CohortError> {
        if prefix_fd < 0 || prefix_argument.is_empty() || prefix_argument.contains(&0) {
            return Err(CohortError::InvalidPrefix);
        }
        let prefix = crate::inherited_fd::duplicate_cloexec(prefix_fd, 3)
            .map_err(|error| CohortError::Io("fcntl(F_DUPFD_CLOEXEC)", error))?;
        let observed = identity(prefix.as_raw_fd())?;
        if file_type(observed) != libc::S_IFDIR || observed.uid != current_uid() {
            return Err(CohortError::Identity("prefix"));
        }
        Self::acquire_owned(prefix, prefix_argument.to_vec(), init_pid)
    }

    fn acquire_owned(
        prefix: OwnedFd,
        prefix_argument: Vec<u8>,
        init_pid: libc::pid_t,
    ) -> Result<Self, CohortError> {
        if init_pid <= 0 {
            return Err(CohortError::Protocol("invalid init pid"));
        }
        #[cfg(not(test))]
        let _ = &prefix_argument;
        let initial_prefix_identity = identity(prefix.as_raw_fd())?;
        let (lock, lock_identity) = acquire_lock(prefix.as_fd())?;
        let prefix_state = acquire_prefix_state(prefix.as_fd(), initial_prefix_identity)?;
        ensure_endpoint_parents(prefix.as_fd())?;
        // Creating a direct child changes a directory's link count. Refresh
        // the retained identity only after the finite bootstrap parents exist;
        // device/inode and the typed prefix-state binding remain unchanged.
        let prefix_identity = identity(prefix.as_raw_fd())?;
        revalidate_prefix_state(prefix.as_fd(), prefix_identity, &prefix_state)?;
        let mut authority = Self {
            prefix,
            prefix_identity,
            lock,
            lock_identity,
            prefix_state,
            init_pid: None,
            endpoints: BTreeMap::new(),
            endpoint_owners: BTreeMap::new(),
            log: None,
            session_root_pid: init_pid,
            #[cfg(test)]
            prefix_argument,
            cleanup_on_drop: true,
            #[cfg(test)]
            allow_test_peer: false,
            #[cfg(test)]
            dynamic_fault: None,
            #[cfg(test)]
            fail_log_after_create: false,
        };
        authority.publish_init_pid(init_pid)?;
        Ok(authority)
    }

    fn revalidate(&self) -> Result<(), CohortError> {
        let observed = identity(self.prefix.as_raw_fd())?;
        if observed.device != self.prefix_identity.device
            || observed.inode != self.prefix_identity.inode
            || observed.mode != self.prefix_identity.mode
            || observed.uid != self.prefix_identity.uid
            || observed.gid != self.prefix_identity.gid
        {
            return Err(CohortError::Identity("prefix"));
        }
        revalidate_lock(self.prefix.as_fd(), self.lock.as_fd(), self.lock_identity)?;
        revalidate_prefix_state(
            self.prefix.as_fd(),
            self.prefix_identity,
            &self.prefix_state,
        )
    }

    fn disarm_drop_cleanup(&mut self) {
        self.cleanup_on_drop = false;
    }

    #[cfg(not(test))]
    fn worker_fds(
        &self,
        listener: &OwnedFd,
        child_control: &OwnedFd,
    ) -> Result<[RawFd; CONTROLLER_WORKER_FD_COUNT], CohortError> {
        let init_pid = self
            .init_pid
            .as_ref()
            .ok_or(CohortError::Protocol("worker init pid"))?;
        let dserver = self
            .endpoints
            .get(&EndpointKey::Static(CohortEndpoint::DarlingServer))
            .ok_or(CohortError::Protocol("worker Darlingserver endpoint"))?;
        let control = self
            .endpoints
            .get(&EndpointKey::Static(CohortEndpoint::Control))
            .ok_or(CohortError::Protocol("worker control endpoint"))?;
        let log = self
            .log
            .as_ref()
            .ok_or(CohortError::Protocol("worker log"))?;
        Ok([
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.prefix_state.object.as_raw_fd(),
            init_pid.object.as_raw_fd(),
            dserver.parent.as_raw_fd(),
            dserver.object.as_raw_fd(),
            control.parent.as_raw_fd(),
            control.object.as_raw_fd(),
            log.parent.as_raw_fd(),
            log.object.as_raw_fd(),
            listener.as_raw_fd(),
            child_control.as_raw_fd(),
        ])
    }

    #[cfg(not(test))]
    fn from_worker_fds(
        mut fds: Vec<OwnedFd>,
        session_root_pid: libc::pid_t,
    ) -> Result<(Self, OwnedFd, OwnedFd), CohortError> {
        if fds.len() != CONTROLLER_WORKER_FD_COUNT || session_root_pid <= 0 {
            return Err(CohortError::Protocol("worker descriptor count"));
        }
        let child_control = fds
            .pop()
            .ok_or(CohortError::Protocol("worker control transport"))?;
        let listener = fds.pop().ok_or(CohortError::Protocol("worker listener"))?;
        let log_object = fds
            .pop()
            .ok_or(CohortError::Protocol("worker log object"))?;
        let log_parent = fds
            .pop()
            .ok_or(CohortError::Protocol("worker log parent"))?;
        let control_object = fds.pop().ok_or(CohortError::Protocol("worker endpoint"))?;
        let control_parent = fds
            .pop()
            .ok_or(CohortError::Protocol("worker endpoint parent"))?;
        let dserver_object = fds.pop().ok_or(CohortError::Protocol("worker endpoint"))?;
        let dserver_parent = fds
            .pop()
            .ok_or(CohortError::Protocol("worker endpoint parent"))?;
        let init_object = fds.pop().ok_or(CohortError::Protocol("worker init pid"))?;
        let state_object = fds
            .pop()
            .ok_or(CohortError::Protocol("worker prefix state"))?;
        let lock = fds.pop().ok_or(CohortError::Protocol("worker lock"))?;
        let prefix = fds.pop().ok_or(CohortError::Protocol("worker prefix"))?;
        let prefix_identity = identity(prefix.as_raw_fd())?;
        let lock_identity = identity(lock.as_raw_fd())?;
        revalidate_lock(prefix.as_fd(), lock.as_fd(), lock_identity)?;
        let state_identity = identity(state_object.as_raw_fd())?;
        let state_content = read_bounded(state_object.as_fd(), PREFIX_STATE_MAX_BYTES)?;
        let (state_name, generation) =
            if named_identity(prefix.as_fd(), PREFIX_STATE_V3_NAME)? == Some(state_identity) {
                (
                    PREFIX_STATE_V3_NAME.to_vec(),
                    parse_prefix_state(&state_content, prefix_identity)?,
                )
            } else {
                return Err(CohortError::Identity("worker prefix state"));
            };
        let init_identity = identity(init_object.as_raw_fd())?;
        if named_identity(prefix.as_fd(), INIT_PID_NAME)? != Some(init_identity) {
            return Err(CohortError::Identity("worker init pid"));
        }
        let endpoint = |kind: CohortEndpoint,
                        parent: OwnedFd,
                        object: OwnedFd|
         -> Result<PublishedEndpoint, CohortError> {
            let spec = kind
                .spec()
                .ok_or(CohortError::Protocol("worker endpoint spec"))?;
            let parent_identity = identity(parent.as_raw_fd())?;
            let object_identity = identity(object.as_raw_fd())?;
            if named_identity(parent.as_fd(), spec.name)? != Some(object_identity) {
                return Err(CohortError::Identity("worker endpoint"));
            }
            Ok(PublishedEndpoint {
                parent,
                parent_identity,
                object,
                identity: object_identity,
                name: spec.name.to_vec(),
                dynamic_directory: None,
                endpoint_linked: true,
            })
        };
        let dserver = endpoint(
            CohortEndpoint::DarlingServer,
            dserver_parent,
            dserver_object,
        )?;
        let control = endpoint(CohortEndpoint::Control, control_parent, control_object)?;
        let log_identity = identity(log_object.as_raw_fd())?;
        let log_parent_identity = identity(log_parent.as_raw_fd())?;
        if named_identity(log_parent.as_fd(), DSERVER_LOG_NAME)? != Some(log_identity) {
            return Err(CohortError::Identity("worker log"));
        }
        let mut endpoints = BTreeMap::new();
        endpoints.insert(EndpointKey::Static(CohortEndpoint::DarlingServer), dserver);
        endpoints.insert(EndpointKey::Static(CohortEndpoint::Control), control);
        Ok((
            Self {
                prefix,
                prefix_identity,
                lock,
                lock_identity,
                prefix_state: RetainedState {
                    object: state_object,
                    identity: state_identity,
                    content: state_content,
                    name: state_name,
                    generation,
                },
                init_pid: Some(RetainedFile {
                    object: init_object,
                    identity: init_identity,
                }),
                endpoints,
                endpoint_owners: BTreeMap::new(),
                log: Some(PublishedLog {
                    parent: log_parent,
                    parent_identity: log_parent_identity,
                    object: log_object,
                    identity: log_identity,
                    name: DSERVER_LOG_NAME.to_vec(),
                }),
                session_root_pid,
                cleanup_on_drop: true,
            },
            listener,
            child_control,
        ))
    }

    fn publish_log(&mut self) -> Result<OwnedFd, CohortError> {
        self.revalidate()?;
        if self.log.is_some() {
            return Err(CohortError::EndpointExists);
        }
        let parent = open_directory_chain(self.prefix.as_fd(), DSERVER_LOG_PARENT)?;
        let parent_identity = identity(parent.as_raw_fd())?;
        let name = DSERVER_LOG_NAME;
        let existing = named_identity(parent.as_fd(), name)?;
        if let Some(expected) = existing {
            // Validate the named object before asking the kernel for a writer.
            // In particular, opening a FIFO O_WRONLY can block indefinitely.
            validate_owned(expected, libc::S_IFREG, Some(0o644))?;
        }
        let (writer, created) = if existing.is_some() {
            (
                openat(
                    parent.as_fd(),
                    name,
                    libc::O_WRONLY | libc::O_APPEND | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0,
                )?,
                None,
            )
        } else {
            let writer = openat(
                parent.as_fd(),
                name,
                libc::O_WRONLY
                    | libc::O_APPEND
                    | libc::O_CREAT
                    | libc::O_EXCL
                    | libc::O_NOFOLLOW
                    | libc::O_CLOEXEC,
                0o644,
            )?;
            let created = identity(writer.as_raw_fd())?;
            (writer, Some(created))
        };
        let publication = (|| {
            // Creation mode is filtered by the process umask.  Normalize the
            // exact newly-created inode before it can be transferred.
            if created.is_some() {
                rustix::fs::fchmod(&writer, rustix::fs::Mode::from_raw_mode(0o644)).map_err(
                    |error| {
                        CohortError::Io(
                            "fchmod(new Darlingserver log)",
                            io::Error::from_raw_os_error(error.raw_os_error()),
                        )
                    },
                )?;
            }
            #[cfg(test)]
            if created.is_some() && self.fail_log_after_create {
                return Err(CohortError::Protocol("injected post-create log failure"));
            }
            let retained = openat(
                parent.as_fd(),
                name,
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            )?;
            let expected = identity(retained.as_raw_fd())?;
            validate_owned(expected, libc::S_IFREG, Some(0o644))?;
            if identity(writer.as_raw_fd())? != expected
                || named_identity(parent.as_fd(), name)? != Some(expected)
            {
                return Err(CohortError::Identity("Darlingserver log publication"));
            }
            Ok((retained, expected))
        })();
        let (retained, expected) = match publication {
            Ok(publication) => publication,
            Err(error) => {
                if let Some(created) = created {
                    rollback_created_log(parent.as_fd(), name, created)?;
                }
                return Err(error);
            }
        };
        if let Some(created) = created {
            if expected.inode_key() != created.inode_key() {
                rollback_created_log(parent.as_fd(), name, created)?;
                return Err(CohortError::Identity("new Darlingserver log identity"));
            }
        }
        self.log = Some(PublishedLog {
            parent,
            parent_identity,
            object: retained,
            identity: expected,
            name: name.to_vec(),
        });
        Ok(writer)
    }

    fn validate_logs(&mut self) -> Result<(), CohortError> {
        self.revalidate()?;
        if let Some(log) = &self.log {
            if identity(log.parent.as_raw_fd())? != log.parent_identity
                || identity(log.object.as_raw_fd())? != log.identity
                || named_identity(log.parent.as_fd(), &log.name)? != Some(log.identity)
            {
                return Err(CohortError::Identity("Darlingserver log replacement"));
            }
        }
        self.log = None;
        Ok(())
    }

    fn publish_init_pid(&mut self, init_pid: libc::pid_t) -> Result<(), CohortError> {
        self.revalidate()?;
        if let Some(existing) = named_identity(self.prefix.as_fd(), INIT_PID_NAME)? {
            validate_owned(existing, libc::S_IFREG, Some(0o600))?;
            let name = component(INIT_PID_NAME)?;
            rustix::fs::unlinkat(&self.prefix, name.as_c_str(), rustix::fs::AtFlags::empty())
                .map_err(|error| {
                    CohortError::Io(
                        "unlinkat(stale init pid)",
                        io::Error::from_raw_os_error(error.raw_os_error()),
                    )
                })?;
        }
        let temporary = format!(".init.pid.lifecycle-{}", std::process::id());
        let temporary_bytes = temporary.as_bytes();
        let file = openat(
            self.prefix.as_fd(),
            temporary_bytes,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )?;
        let (init_identity, _) = process_identity(init_pid)?;
        let bytes = format!("{init_pid} {}\n", init_identity.starttime);
        let mut published_identity = None;
        let result = (|| {
            write_all(file.as_fd(), bytes.as_bytes())?;
            rustix::fs::fsync(&file).map_err(|error| {
                CohortError::Io(
                    "fsync(init pid)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                )
            })?;
            let retained = openat(
                self.prefix.as_fd(),
                temporary_bytes,
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            )?;
            let retained_identity = identity(retained.as_raw_fd())?;
            validate_owned(retained_identity, libc::S_IFREG, Some(0o600))?;
            published_identity = Some(retained_identity);
            let temporary = component(temporary_bytes)?;
            let destination = component(INIT_PID_NAME)?;
            rustix::fs::renameat(
                &self.prefix,
                temporary.as_c_str(),
                &self.prefix,
                destination.as_c_str(),
            )
            .map_err(|error| {
                CohortError::Io(
                    "renameat(init pid)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                )
            })?;
            if named_identity(self.prefix.as_fd(), INIT_PID_NAME)? != Some(retained_identity) {
                return Err(CohortError::Identity("init pid publication"));
            }
            self.init_pid = Some(RetainedFile {
                object: retained,
                identity: retained_identity,
            });
            Ok(())
        })();
        if result.is_err() {
            if let Ok(name) = component(temporary_bytes) {
                let _ = rustix::fs::unlinkat(
                    &self.prefix,
                    name.as_c_str(),
                    rustix::fs::AtFlags::empty(),
                );
            }
            if let Some(expected) = published_identity {
                if named_identity(self.prefix.as_fd(), INIT_PID_NAME)
                    .ok()
                    .flatten()
                    == Some(expected)
                {
                    if let Ok(name) = component(INIT_PID_NAME) {
                        let _ = rustix::fs::unlinkat(
                            &self.prefix,
                            name.as_c_str(),
                            rustix::fs::AtFlags::empty(),
                        );
                    }
                }
            }
        }
        result
    }

    fn prepare_publication(
        &mut self,
        key: EndpointKey,
    ) -> Result<OwnerDeathTransition, CohortError> {
        if !self.endpoints.contains_key(&key) {
            if self.endpoint_owners.contains_key(&key) {
                return Err(CohortError::Identity("endpoint owner without endpoint"));
            }
            return Ok(OwnerDeathTransition::Fresh);
        }
        let Some(owner) = self.endpoint_owners.get(&key) else {
            return Ok(OwnerDeathTransition::RetainedAlive);
        };
        match owner_process_state(owner._process.as_raw_fd())? {
            OwnerProcessState::Alive => Ok(OwnerDeathTransition::RetainedAlive),
            OwnerProcessState::Gone => {
                // The retained pidfd proves owner death without consulting a
                // reusable numeric PID.  Retirement still requires the exact
                // retained endpoint inode under the session lease.
                self.retire_key(key)?;
                Ok(OwnerDeathTransition::RetiredGone)
            }
        }
    }

    fn cleanup_dead_per_user_endpoints(&mut self) -> Result<(), CohortError> {
        let keys = self
            .endpoint_owners
            .iter()
            .filter_map(|(key, owner)| {
                matches!(key, EndpointKey::PerUser(_)).then_some((*key, owner._process.as_raw_fd()))
            })
            .collect::<Vec<_>>();
        for (key, pidfd) in keys {
            if owner_process_state(pidfd)? == OwnerProcessState::Gone {
                self.retire_key(key)?;
            }
        }
        Ok(())
    }

    fn create_dynamic_directory(
        &self,
        peer: PeerIdentity,
    ) -> Result<(OwnedFd, RetainedDirectory), CohortError> {
        let parent = open_directory_chain(self.prefix.as_fd(), PER_USER_PARENT)?;
        for _attempt in 0..DYNAMIC_DIRECTORY_ATTEMPTS {
            let nonce = random_nonce()?;
            let name = format!(
                "launchd-{}-{:02x}{:02x}{:02x}{:02x}",
                peer.pid, nonce[0], nonce[1], nonce[2], nonce[3]
            )
            .into_bytes();
            let component = component(&name)?;
            if let Err(error) = rustix::fs::mkdirat(
                &parent,
                component.as_c_str(),
                rustix::fs::Mode::from_raw_mode(0o700),
            ) {
                if error == rustix::io::Errno::EXIST {
                    continue;
                }
                return Err(CohortError::Io(
                    "mkdirat(per-user launchd)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                ));
            }
            #[cfg(test)]
            if self.dynamic_fault == Some(DynamicPublicationFault::AfterDirectoryCreate) {
                let _ = rustix::fs::unlinkat(
                    &parent,
                    component.as_c_str(),
                    rustix::fs::AtFlags::REMOVEDIR,
                );
                return Err(CohortError::Io(
                    "fault(after dynamic directory create)",
                    io::Error::from_raw_os_error(libc::EINTR),
                ));
            }
            let directory = openat(
                parent.as_fd(),
                &name,
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            );
            let directory = match directory {
                Ok(directory) => directory,
                Err(error) => {
                    let _ = rustix::fs::unlinkat(
                        &parent,
                        component.as_c_str(),
                        rustix::fs::AtFlags::REMOVEDIR,
                    );
                    return Err(error);
                }
            };
            let validated = (|| {
                let directory_identity = identity(directory.as_raw_fd())?;
                if file_type(directory_identity) != libc::S_IFDIR
                    || directory_identity.uid != current_uid()
                    || directory_identity.mode & 0o777 != 0o700
                    || directory_identity.nlink != 2
                {
                    return Err(CohortError::Identity("per-user launchd directory"));
                }
                if named_identity(parent.as_fd(), &name)? != Some(directory_identity) {
                    return Err(CohortError::Identity("per-user launchd directory"));
                }
                Ok(directory_identity)
            })();
            let directory_identity = match validated {
                Ok(identity) => identity,
                Err(error) => {
                    let _ = rustix::fs::unlinkat(
                        &parent,
                        component.as_c_str(),
                        rustix::fs::AtFlags::REMOVEDIR,
                    );
                    return Err(error);
                }
            };
            return Ok((
                directory,
                RetainedDirectory {
                    parent_identity: identity(parent.as_raw_fd())?,
                    parent,
                    identity: directory_identity,
                    name,
                },
            ));
        }
        Err(CohortError::Protocol(
            "dynamic directory attempts exhausted",
        ))
    }

    fn publish_key(&mut self, key: EndpointKey) -> Result<(OwnedFd, Vec<u8>), CohortError> {
        self.revalidate()?;
        if matches!(key, EndpointKey::PerUser(_)) {
            self.cleanup_dead_per_user_endpoints()?;
        }
        if self.prepare_publication(key)? == OwnerDeathTransition::RetainedAlive {
            return Err(CohortError::EndpointExists);
        }
        let (spec, parent, dynamic_directory, guest_path) = match key {
            EndpointKey::Static(kind) => {
                let spec = kind
                    .spec()
                    .ok_or(CohortError::Protocol("dynamic endpoint key"))?;
                let parent = open_directory_chain(self.prefix.as_fd(), spec.parent)?;
                (spec, parent, None, Vec::new())
            }
            EndpointKey::PerUser(peer) => {
                let (parent, directory) = self.create_dynamic_directory(peer)?;
                let mut guest_path = b"/private/var/tmp/".to_vec();
                guest_path.extend_from_slice(&directory.name);
                guest_path.extend_from_slice(b"/sock");
                if guest_path.len() >= DYNAMIC_PATH_CAPACITY {
                    return Err(CohortError::Protocol("dynamic endpoint path capacity"));
                }
                (
                    EndpointSpec {
                        parent: &[],
                        name: b"sock",
                        socket_type: libc::SOCK_STREAM,
                        nonblocking: false,
                        listen_backlog: Some(libc::SOMAXCONN),
                        mode: 0o600,
                    },
                    parent,
                    Some(directory),
                    guest_path,
                )
            }
        };
        let parent_identity = identity(parent.as_raw_fd())?;
        if let Some(stale) = named_identity(parent.as_fd(), spec.name)? {
            validate_owned(stale, libc::S_IFSOCK, Some(spec.mode))?;
            let name = component(spec.name)?;
            rustix::fs::unlinkat(&parent, name.as_c_str(), rustix::fs::AtFlags::empty()).map_err(
                |error| {
                    CohortError::Io(
                        "unlinkat(stale endpoint)",
                        io::Error::from_raw_os_error(error.raw_os_error()),
                    )
                },
            )?;
        }
        let socket_type = match spec.socket_type {
            libc::SOCK_STREAM => rustix::net::SocketType::STREAM,
            libc::SOCK_DGRAM => rustix::net::SocketType::DGRAM,
            libc::SOCK_SEQPACKET => rustix::net::SocketType::SEQPACKET,
            _ => return Err(CohortError::Protocol("endpoint socket type")),
        };
        let mut socket_flags = rustix::net::SocketFlags::CLOEXEC;
        if spec.nonblocking {
            socket_flags |= rustix::net::SocketFlags::NONBLOCK;
        }
        let socket = rustix::net::socket_with(
            rustix::net::AddressFamily::UNIX,
            socket_type,
            socket_flags,
            None,
        )
        .map_err(|error| {
            CohortError::Io(
                "socket(endpoint)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;
        let path = format!(
            "/proc/self/fd/{}/{}",
            parent.as_raw_fd(),
            String::from_utf8_lossy(spec.name)
        );
        let address = rustix::net::SocketAddrUnix::new(path).map_err(|error| {
            CohortError::Io(
                "endpoint path",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;
        rustix::net::bind(&socket, &address).map_err(|error| {
            CohortError::Io(
                "bind(endpoint)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;
        // Bind is the first namespace mutation.  Retain the exact inode before
        // any later fallible setup so every subsequent failure can roll back
        // the object we created rather than whatever may occupy its name.
        let mut bound_inode = None;
        let prepared = (|| {
            let object = openat(
                parent.as_fd(),
                spec.name,
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            )?;
            bound_inode = Some(identity(object.as_raw_fd())?.inode_key());
            let name = component(spec.name)?;
            rustix::fs::chmodat(
                &parent,
                name.as_c_str(),
                rustix::fs::Mode::from_raw_mode(spec.mode),
                rustix::fs::AtFlags::empty(),
            )
            .map_err(|error| {
                CohortError::Io(
                    "fchmodat(endpoint)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                )
            })?;
            if let Some(backlog) = spec.listen_backlog {
                rustix::net::listen(&socket, backlog).map_err(|error| {
                    CohortError::Io(
                        "listen(endpoint)",
                        io::Error::from_raw_os_error(error.raw_os_error()),
                    )
                })?;
            }
            let endpoint_identity = identity(object.as_raw_fd())?;
            validate_owned(endpoint_identity, libc::S_IFSOCK, Some(spec.mode))?;
            if named_identity(parent.as_fd(), spec.name)? != Some(endpoint_identity) {
                return Err(CohortError::Identity("endpoint publication"));
            }
            Ok((object, endpoint_identity))
        })();
        let (object, endpoint_identity) = match prepared {
            Ok(value) => value,
            Err(error) => {
                // The exact lifecycle lease excludes every compatible writer.
                // Once the retained inode exists, never delete a different
                // object even if a non-cooperative writer violated the v1
                // threat model.  If O_PATH acquisition itself failed, the
                // just-bound name is still transaction-owned under the lease.
                let named = named_identity(parent.as_fd(), spec.name).ok().flatten();
                let owned = match (bound_inode, named) {
                    (Some(expected), Some(actual)) => actual.inode_key() == expected,
                    (None, Some(_)) => true,
                    _ => false,
                };
                if owned {
                    if let Ok(name) = component(spec.name) {
                        let _ = rustix::fs::unlinkat(
                            &parent,
                            name.as_c_str(),
                            rustix::fs::AtFlags::empty(),
                        );
                    }
                }
                if let Some(directory) = dynamic_directory.as_ref() {
                    if identity(directory.parent.as_raw_fd()).ok()
                        == Some(directory.parent_identity)
                        && identity(parent.as_raw_fd()).ok() == Some(directory.identity)
                        && named_identity(directory.parent.as_fd(), &directory.name)
                            .ok()
                            .flatten()
                            == Some(directory.identity)
                    {
                        if let Ok(name) = component(&directory.name) {
                            let _ = rustix::fs::unlinkat(
                                &directory.parent,
                                name.as_c_str(),
                                rustix::fs::AtFlags::REMOVEDIR,
                            );
                        }
                    }
                }
                return Err(error);
            }
        };
        self.endpoints.insert(
            key,
            PublishedEndpoint {
                parent,
                parent_identity,
                object,
                identity: endpoint_identity,
                name: spec.name.to_vec(),
                dynamic_directory,
                endpoint_linked: true,
            },
        );
        Ok((socket, guest_path))
    }

    fn publish(&mut self, kind: CohortEndpoint) -> Result<OwnedFd, CohortError> {
        self.publish_key(EndpointKey::Static(kind))
            .map(|(socket, _guest_path)| socket)
    }

    fn authorize_peer(
        &self,
        kind: CohortEndpoint,
        operation: u16,
        peer: PeerAuthority,
    ) -> Result<(EndpointKey, PeerAuthority), CohortError> {
        #[cfg(test)]
        if self.allow_test_peer {
            return Ok((
                if kind == CohortEndpoint::PerUserLaunchd {
                    EndpointKey::PerUser(peer.identity)
                } else {
                    EndpointKey::Static(kind)
                },
                peer,
            ));
        }
        let (identity, parent) = process_identity(peer.identity.pid)?;
        if identity != peer.identity {
            return Err(CohortError::Protocol("peer identity drift"));
        }
        if operation == 2 {
            let key = if kind == CohortEndpoint::PerUserLaunchd {
                EndpointKey::PerUser(peer.identity)
            } else {
                EndpointKey::Static(kind)
            };
            let owner = self
                .endpoint_owners
                .get(&key)
                .ok_or(CohortError::Protocol("endpoint owner"))?;
            return if owner.identity == peer.identity
                && owner_process_state(owner._process.as_raw_fd())? == OwnerProcessState::Alive
            {
                Ok((key, peer))
            } else {
                Err(CohortError::Protocol("endpoint owner"))
            };
        }
        match kind {
            CohortEndpoint::Launchd => {
                let argv = process_argv(peer.identity.pid)?;
                if parent != self.session_root_pid || argv.len() != 1 || argv[0] != b"/sbin/launchd"
                {
                    return Err(CohortError::Protocol("launchd peer identity"));
                }
            }
            CohortEndpoint::Shellspawn => {
                let launchd = self
                    .endpoint_owners
                    .get(&EndpointKey::Static(CohortEndpoint::Launchd))
                    .ok_or(CohortError::Protocol("launchd authority missing"))?;
                let argv = process_argv(peer.identity.pid)?;
                if argv.len() != 1
                    || argv0_basename(&argv) != b"shellspawn"
                    || parent != launchd.identity.pid
                {
                    return Err(CohortError::Protocol("shellspawn peer identity"));
                }
            }
            CohortEndpoint::PerUserLaunchd => {
                let launchd = self
                    .endpoint_owners
                    .get(&EndpointKey::Static(CohortEndpoint::Launchd))
                    .ok_or(CohortError::Protocol("launchd authority missing"))?;
                let argv = process_argv(peer.identity.pid)?;
                if argv.len() != 1 || argv[0] != b"/sbin/launchd" || parent != launchd.identity.pid
                {
                    return Err(CohortError::Protocol("per-user launchd peer identity"));
                }
            }
            CohortEndpoint::DarlingServer => {
                return Err(CohortError::Protocol(
                    "Darlingserver is not a transport endpoint",
                ));
            }
            CohortEndpoint::Control => {
                return Err(CohortError::Protocol("control endpoint is internal"));
            }
        }
        let key = if kind == CohortEndpoint::PerUserLaunchd {
            EndpointKey::PerUser(peer.identity)
        } else {
            EndpointKey::Static(kind)
        };
        Ok((key, peer))
    }

    fn retire_key(&mut self, key: EndpointKey) -> Result<(), CohortError> {
        self.revalidate()?;
        let endpoint = self
            .endpoints
            .get_mut(&key)
            .ok_or(CohortError::EndpointMissing)?;
        if endpoint.endpoint_linked {
            if !same_retained_object(
                identity(endpoint.parent.as_raw_fd())?,
                endpoint.parent_identity,
            ) || identity(endpoint.object.as_raw_fd())? != endpoint.identity
                || named_identity(endpoint.parent.as_fd(), &endpoint.name)?
                    != Some(endpoint.identity)
            {
                return Err(CohortError::Identity("endpoint replacement"));
            }
            let name = component(&endpoint.name)?;
            rustix::fs::unlinkat(
                &endpoint.parent,
                name.as_c_str(),
                rustix::fs::AtFlags::empty(),
            )
            .map_err(|error| {
                CohortError::Io(
                    "unlinkat(endpoint)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                )
            })?;
            endpoint.endpoint_linked = false;
        }
        if let Some(directory) = endpoint.dynamic_directory.as_ref() {
            if !same_retained_object(
                identity(directory.parent.as_raw_fd())?,
                directory.parent_identity,
            ) || !same_retained_object(
                identity(endpoint.parent.as_raw_fd())?,
                directory.identity,
            ) || named_identity(directory.parent.as_fd(), &directory.name)?
                != Some(directory.identity)
            {
                return Err(CohortError::Identity("dynamic directory replacement"));
            }
            let name = component(&directory.name)?;
            rustix::fs::unlinkat(
                &directory.parent,
                name.as_c_str(),
                rustix::fs::AtFlags::REMOVEDIR,
            )
            .map_err(|error| {
                CohortError::Io(
                    "unlinkat(per-user launchd directory)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                )
            })?;
        }
        self.endpoints.remove(&key);
        self.endpoint_owners.remove(&key);
        Ok(())
    }

    fn cleanup_all(&mut self) -> Result<(), CohortError> {
        let mut first_error = None;
        if let Err(error) = self.validate_logs() {
            first_error = Some(error);
        }
        let keys = self.endpoints.keys().copied().collect::<Vec<_>>();
        for key in keys {
            if self.endpoints.contains_key(&key) {
                if let Err(error) = self.retire_key(key) {
                    if first_error.is_none() {
                        first_error = Some(error);
                    }
                }
            }
        }
        let init_cleanup = (|| {
            self.revalidate()?;
            if let Some(retained) = self.init_pid.as_ref() {
                if identity(retained.object.as_raw_fd())? != retained.identity
                    || named_identity(self.prefix.as_fd(), INIT_PID_NAME)?
                        != Some(retained.identity)
                {
                    return Err(CohortError::Identity("init pid replacement"));
                }
                let name = component(INIT_PID_NAME)?;
                rustix::fs::unlinkat(&self.prefix, name.as_c_str(), rustix::fs::AtFlags::empty())
                    .map_err(|error| {
                    CohortError::Io(
                        "unlinkat(init pid)",
                        io::Error::from_raw_os_error(error.raw_os_error()),
                    )
                })?;
                self.init_pid = None;
            }
            Ok(())
        })();
        if let Err(error) = init_cleanup {
            if first_error.is_none() {
                first_error = Some(error);
            }
        }
        first_error.map_or(Ok(()), Err)
    }
}

impl Drop for SessionAuthority {
    fn drop(&mut self) {
        if self.cleanup_on_drop {
            let _ = self.cleanup_all();
        }
    }
}

#[derive(Clone, Copy)]
#[repr(C)]
struct WireRequest {
    magic: [u8; 8],
    version: u16,
    operation: u16,
    endpoint: u16,
    reserved: u16,
    nonce: [u8; NONCE_BYTES],
}

#[derive(Clone, Copy)]
#[repr(C)]
struct WireResponse {
    magic: [u8; 8],
    version: u16,
    status: i16,
    endpoint: u16,
    has_fd: u16,
    phase: u16,
    reserved: u16,
    error: i32,
    device: u64,
    inode: u64,
    path_len: u16,
    path: [u8; DYNAMIC_PATH_CAPACITY],
}

fn wire_response(
    status: i16,
    endpoint: u16,
    phase: u16,
    error: i32,
    passed: Option<FileIdentity>,
    guest_path: &[u8],
) -> Result<WireResponse, CohortError> {
    if guest_path.len() >= DYNAMIC_PATH_CAPACITY {
        return Err(CohortError::Protocol("dynamic endpoint path capacity"));
    }
    let mut path = [0u8; DYNAMIC_PATH_CAPACITY];
    path[..guest_path.len()].copy_from_slice(guest_path);
    Ok(WireResponse {
        magic: PROTOCOL_MAGIC,
        version: PROTOCOL_VERSION,
        status,
        endpoint,
        has_fd: u16::from(passed.is_some()),
        phase,
        reserved: 0,
        error,
        device: passed.map_or(0, |identity| identity.device),
        inode: passed.map_or(0, |identity| identity.inode),
        path_len: guest_path.len() as u16,
        path,
    })
}

fn response_error(error: &CohortError) -> i32 {
    match error {
        CohortError::Io(_, _) => RESPONSE_ERROR_IO,
        CohortError::InvalidPrefix | CohortError::Identity(_) => RESPONSE_ERROR_IDENTITY,
        CohortError::LockBusy => RESPONSE_ERROR_LOCK_BUSY,
        CohortError::Protocol(_) => RESPONSE_ERROR_PROTOCOL,
        CohortError::EndpointExists => RESPONSE_ERROR_ENDPOINT_EXISTS,
        CohortError::EndpointMissing => RESPONSE_ERROR_ENDPOINT_MISSING,
        CohortError::Thread | CohortError::Process => RESPONSE_ERROR_PROCESS,
    }
}

fn receive_request(fd: RawFd) -> Result<WireRequest, CohortError> {
    cohort_ffi::receive_wire_request(fd)
}

fn send_response(
    fd: RawFd,
    response: WireResponse,
    passed_fd: Option<RawFd>,
) -> Result<(), CohortError> {
    cohort_ffi::send_wire_response(fd, response, passed_fd)
}

fn peer_credentials(fd: RawFd) -> Result<libc::ucred, CohortError> {
    cohort_ffi::socket_peer_credentials(fd)
}

fn wait_for_io(fd: RawFd, events: i16) -> Result<(), CohortError> {
    wait_for_io_timeout(fd, events, REQUEST_TIMEOUT_MS)
}

fn wait_for_io_timeout(fd: RawFd, events: i16, timeout_ms: c_int) -> Result<(), CohortError> {
    let retained = crate::inherited_fd::duplicate_cloexec(fd, 3)
        .map_err(|error| CohortError::Io("duplicate(poll target)", error))?;
    let wanted = rustix::event::PollFlags::from_bits_retain(events as u16);
    let deadline = Instant::now() + Duration::from_millis(timeout_ms as u64);
    loop {
        let now = Instant::now();
        if now >= deadline {
            return Err(CohortError::Protocol("client request timeout"));
        }
        let remaining = deadline.saturating_duration_since(now);
        let timeout = rustix::event::Timespec {
            tv_sec: remaining.as_secs().try_into().unwrap_or(i64::MAX),
            tv_nsec: remaining.subsec_nanos().into(),
        };
        let mut descriptors = [rustix::event::PollFd::new(&retained, wanted)];
        let result = match rustix::event::poll(&mut descriptors, Some(&timeout)) {
            Ok(result) => result,
            Err(rustix::io::Errno::INTR) => {
                continue;
            }
            Err(error) => {
                return Err(CohortError::Io(
                    "poll(client)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                ))
            }
        };
        if result == 0 {
            return Err(CohortError::Protocol("client request timeout"));
        }
        if descriptors[0].revents().intersects(wanted) {
            return Ok(());
        }
        return Err(CohortError::Protocol("client socket failure"));
    }
}

fn serve_client(
    authority: &mut SessionAuthority,
    client: RawFd,
    nonce: &[u8; NONCE_BYTES],
) -> Result<(), CohortError> {
    wait_for_io(client, libc::POLLIN)?;
    let credentials = peer_credentials(client)?;
    if credentials.uid != current_uid() {
        return Err(CohortError::Protocol("peer uid"));
    }
    let request = receive_request(client)?;
    if request.magic != PROTOCOL_MAGIC
        || request.version != PROTOCOL_VERSION
        || request.reserved != 0
        || request.nonce != *nonce
    {
        return Err(CohortError::Protocol("request envelope"));
    }
    let kind = CohortEndpoint::from_wire(request.endpoint)
        .ok_or(CohortError::Protocol("endpoint kind"))?;
    if request.operation != OPERATION_PUBLISH && request.operation != OPERATION_RETIRE {
        return Err(CohortError::Protocol("operation"));
    }
    let peer_process = peer_pidfd(client, credentials.pid)?;
    let (peer_identity, _) = process_identity(credentials.pid)?;
    if owner_process_state(peer_process.as_raw_fd())? != OwnerProcessState::Alive {
        return Err(CohortError::Protocol("peer exited during identity bind"));
    }
    let (key, peer) = authority.authorize_peer(
        kind,
        request.operation,
        PeerAuthority {
            identity: peer_identity,
            _process: peer_process,
        },
    )?;
    if request.operation == OPERATION_PUBLISH {
        let (socket, guest_path) = match authority.publish_key(key) {
            Ok(publication) => publication,
            Err(error) => {
                eprintln!("lifecycle cohort operation refused: {error}");
                send_response(
                    client,
                    wire_response(
                        -1,
                        request.endpoint,
                        RESPONSE_PHASE_PUBLISH,
                        response_error(&error),
                        None,
                        &[],
                    )?,
                    None,
                )?;
                return Ok(());
            }
        };
        let socket_identity = match identity(socket.as_raw_fd()) {
            Ok(identity) => identity,
            Err(error) => {
                let _ = authority.retire_key(key);
                return Err(error);
            }
        };
        let pending = wire_response(
            0,
            request.endpoint,
            RESPONSE_PHASE_PUBLISH,
            RESPONSE_ERROR_NONE,
            Some(socket_identity),
            &guest_path,
        )?;
        if let Err(error) = send_response(client, pending, Some(socket.as_raw_fd())) {
            let _ = authority.retire_key(key);
            return Err(error);
        }
        let decision = (|| {
            wait_for_io(client, libc::POLLIN)?;
            let decision = receive_request(client)?;
            if decision.magic != PROTOCOL_MAGIC
                || decision.version != PROTOCOL_VERSION
                || decision.reserved != 0
                || decision.nonce != *nonce
                || decision.endpoint != request.endpoint
                || (decision.operation != OPERATION_COMMIT && decision.operation != OPERATION_ABORT)
            {
                return Err(CohortError::Protocol("publication decision"));
            }
            Ok(decision.operation)
        })();
        match decision {
            Ok(OPERATION_COMMIT) => {
                // Receiving a complete, authenticated COMMIT is the
                // irreversible ownership handoff.  There must be no fallible
                // step between it and recording the owner: the client cannot
                // distinguish a lost final acknowledgement from a rejected
                // commit and must therefore be allowed to rely on the send.
                authority.endpoint_owners.insert(key, peer);
                let committed = wire_response(
                    0,
                    request.endpoint,
                    RESPONSE_PHASE_COMMIT,
                    RESPONSE_ERROR_NONE,
                    None,
                    &[],
                )?;
                // The acknowledgement is diagnostic only.  Once COMMIT was
                // received, delivery failure cannot revoke ownership or
                // unlink the endpoint behind the live consumer's retained FD.
                send_response(client, committed, None)?;
                return Ok(());
            }
            Ok(OPERATION_ABORT) => {
                authority.retire_key(key)?;
                send_response(
                    client,
                    wire_response(
                        0,
                        request.endpoint,
                        RESPONSE_PHASE_ABORT,
                        RESPONSE_ERROR_NONE,
                        None,
                        &[],
                    )?,
                    None,
                )?;
                return Ok(());
            }
            Ok(_) => unreachable!("publication decision was validated"),
            Err(error) => {
                let rollback = authority.retire_key(key);
                return match rollback {
                    Ok(()) => Err(error),
                    Err(rollback) => Err(rollback),
                };
            }
        }
    }

    let result = authority.retire_key(key);
    let status = if result.is_ok() { 0 } else { -1 };
    if let Err(error) = result.as_ref() {
        eprintln!("lifecycle cohort operation refused: {error}");
    }
    let error = result
        .as_ref()
        .err()
        .map_or(RESPONSE_ERROR_NONE, response_error);
    let response = wire_response(
        status,
        request.endpoint,
        RESPONSE_PHASE_RETIRE,
        error,
        None,
        &[],
    )?;
    send_response(client, response, None)?;
    // Operation failures are represented by the typed response.  Returning
    // Ok here prevents the server loop from emitting a second response.
    Ok(())
}

type ServerLoopResult = (SessionAuthority, Result<(), CohortError>, bool);

fn server_loop(
    mut authority: SessionAuthority,
    listener: OwnedFd,
    control: OwnedFd,
    parent_watch: Option<OwnedFd>,
    nonce: [u8; NONCE_BYTES],
) -> ServerLoopResult {
    let mut rejected_requests = 0usize;
    let mut preserve_acknowledged = false;
    let forensic_preserve;
    let result = loop {
        let listener_events = if preserve_acknowledged {
            rustix::event::PollFlags::empty()
        } else {
            rustix::event::PollFlags::IN
        };
        let parent_poll_fd = parent_watch.as_ref().unwrap_or(&control);
        let parent_events = if parent_watch.is_some() {
            rustix::event::PollFlags::IN
        } else {
            rustix::event::PollFlags::empty()
        };
        let mut pollfds = [
            rustix::event::PollFd::new(&listener, listener_events),
            rustix::event::PollFd::new(
                &control,
                rustix::event::PollFlags::IN
                    | rustix::event::PollFlags::HUP
                    | rustix::event::PollFlags::ERR,
            ),
            rustix::event::PollFd::new(parent_poll_fd, parent_events),
        ];
        let timeout = rustix::event::Timespec {
            tv_sec: (REQUEST_TIMEOUT_MS / 1000).into(),
            tv_nsec: ((REQUEST_TIMEOUT_MS % 1000) * 1_000_000).into(),
        };
        if let Err(error) = rustix::event::poll(&mut pollfds, Some(&timeout)) {
            if error == rustix::io::Errno::INTR {
                continue;
            }
            forensic_preserve = true;
            break Err(CohortError::Io(
                "poll(controller)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            ));
        }
        if pollfds[1].revents().intersects(
            rustix::event::PollFlags::IN
                | rustix::event::PollFlags::HUP
                | rustix::event::PollFlags::ERR,
        ) {
            let mut command = [0u8; 2];
            let received =
                rustix::net::recv(&control, &mut command, rustix::net::RecvFlags::empty())
                    .map_or(-1, |(count, _)| count as isize);
            let exact = received == 1;
            if exact && command[0] == CONTROLLER_COMMAND_CLEANUP {
                // Receipt proves the parent's successful send commit. Cleanup
                // remains committed even if the diagnostic ACK is lost.
                let acknowledged = [CONTROLLER_COMMAND_ACK];
                let _ = write_all(control.as_fd(), &acknowledged);
                forensic_preserve = false;
                break Ok(());
            }
            if !exact || command[0] != CONTROLLER_COMMAND_PRESERVE {
                forensic_preserve = true;
                break Ok(());
            }
            let acknowledged = [CONTROLLER_COMMAND_ACK];
            if write_all(control.as_fd(), &acknowledged).is_err() {
                forensic_preserve = true;
                break Ok(());
            }
            if parent_watch.is_some() {
                // Admission is closed after preserve ACK. Keep the production
                // child alive solely to witness parent death; it performs no
                // more endpoint work and cleanup remains disarmed.
                preserve_acknowledged = true;
                continue;
            }
            forensic_preserve = true;
            break Ok(());
        }
        if pollfds[2].revents().contains(rustix::event::PollFlags::IN) {
            forensic_preserve = true;
            break Ok(());
        }
        if !pollfds[0].revents().contains(rustix::event::PollFlags::IN) {
            continue;
        }
        let client = match rustix::net::accept_with(&listener, rustix::net::SocketFlags::CLOEXEC) {
            Ok(client) => client,
            Err(_) => continue,
        };
        if let Err(error) = serve_client(&mut authority, client.as_raw_fd(), &nonce) {
            if rejected_requests == 0 {
                eprintln!("lifecycle cohort request rejected: {error}");
            }
            if let Ok(response) = wire_response(
                -1,
                0,
                RESPONSE_PHASE_REQUEST,
                response_error(&error),
                None,
                &[],
            ) {
                let _ = send_response(client.as_raw_fd(), response, None);
            }
            rejected_requests += 1;
            if rejected_requests == MAX_REJECTED_REQUESTS_PER_SLICE {
                // Invalid same-UID input never consumes a lifetime counter or
                // terminates the authority process.  A bounded cooperative
                // yield prevents a tight malformed-request loop from
                // monopolizing the controller indefinitely.
                thread::yield_now();
                rejected_requests = 0;
            }
        } else {
            rejected_requests = 0;
        }
    };
    if forensic_preserve {
        authority.disarm_drop_cleanup();
    }
    (authority, result, forensic_preserve)
}

#[repr(C)]
pub struct CohortBootstrap {
    pub darlingserver_fd: c_int,
    pub dserver_log_fd: c_int,
    pub control_name_len: u16,
    pub reserved: u16,
    pub control_name: [u8; CONTROL_NAME_CAPACITY],
    pub nonce_hex: [u8; NONCE_HEX_BYTES],
}

#[repr(C)]
#[derive(Clone, Copy)]
#[cfg_attr(test, allow(dead_code))]
struct ControllerWorkerBootstrap {
    version: u32,
    descriptor_count: u32,
    parent_pid: libc::pid_t,
    session_root_pid: libc::pid_t,
    nonce: [u8; NONCE_BYTES],
}

struct ProcessWorker {
    pid: libc::pid_t,
    pidfd: OwnedFd,
}

/// Entry point for the separately installed controller worker binary.
/// Authority arrives only through the fixed inherited bootstrap socket and
/// SCM_RIGHTS; argv and environment carry no authority.
#[cfg(not(test))]
pub fn controller_worker_main() -> i32 {
    if std::env::vars_os().next().is_some() {
        return 1;
    }
    if cohort_ffi::worker_setsid().is_err() {
        return 1;
    }
    let bootstrap = match crate::inherited_fd::duplicate_cloexec(3, 5) {
        Ok(fd) => fd,
        Err(_) => return 1,
    };
    cohort_ffi::close_worker_trampoline_descriptors();
    let credentials = match cohort_ffi::socket_peer_credentials(bootstrap.as_raw_fd()) {
        Ok(value) => value,
        Err(_) => return 1,
    };
    let parent_watch = match cohort_ffi::peer_pidfd_from_socket(bootstrap.as_raw_fd()) {
        Ok(fd) => fd,
        Err(_) => return 1,
    };
    let (envelope, fds) = match cohort_ffi::receive_worker_bootstrap(bootstrap.as_raw_fd()) {
        Ok(value) => value,
        Err(_) => return 1,
    };
    if envelope.version != CONTROLLER_WORKER_PROTOCOL
        || envelope.descriptor_count as usize != CONTROLLER_WORKER_FD_COUNT
        || envelope.parent_pid <= 0
        || envelope.parent_pid != credentials.pid
        || envelope.session_root_pid <= 0
    {
        return 1;
    }
    let (mut authority, listener, child_control) =
        match SessionAuthority::from_worker_fds(fds, envelope.session_root_pid) {
            Ok(value) => value,
            Err(_) => return 1,
        };
    if cohort_ffi::send_worker_ready(bootstrap.as_raw_fd()).is_err() {
        authority.disarm_drop_cleanup();
        return 1;
    }
    let (mut authority, loop_result, forensic_preserve) = server_loop(
        authority,
        listener,
        child_control,
        Some(parent_watch),
        envelope.nonce,
    );
    let cleanup_result = if forensic_preserve {
        authority.disarm_drop_cleanup();
        Ok(())
    } else {
        authority.cleanup_all()
    };
    i32::from(loop_result.is_err() || cleanup_result.is_err())
}

#[cfg(test)]
pub fn controller_worker_main() -> i32 {
    1
}

fn pidfd_open(pid: libc::pid_t) -> Result<OwnedFd, CohortError> {
    let pid = rustix::process::Pid::from_raw(pid).ok_or(CohortError::Protocol("pidfd pid"))?;
    rustix::process::pidfd_open(pid, rustix::process::PidfdFlags::empty()).map_err(|error| {
        CohortError::Io(
            "pidfd_open(controller)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg_attr(not(test), allow(dead_code))]
enum AbandonFault {
    KillError,
    PidfdTimeout,
    WaitpidEintr,
    WaitpidZero,
    WaitpidError,
}

fn abandon_process_worker(
    worker: &ProcessWorker,
    fault: Option<AbandonFault>,
) -> Result<(), CohortError> {
    if fault == Some(AbandonFault::KillError) {
        return Err(CohortError::Io(
            "kill(abandon controller)",
            io::Error::from_raw_os_error(libc::EPERM),
        ));
    }
    let pid =
        rustix::process::Pid::from_raw(worker.pid).ok_or(CohortError::Protocol("worker pid"))?;
    if let Err(error) =
        rustix::process::pidfd_send_signal(&worker.pidfd, rustix::process::Signal::KILL)
    {
        if error != rustix::io::Errno::SRCH {
            return Err(CohortError::Io(
                "kill(abandon controller)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            ));
        }
    }
    if fault == Some(AbandonFault::PidfdTimeout) {
        return Err(CohortError::Protocol("abandon pidfd timeout"));
    }
    wait_for_io(worker.pidfd.as_raw_fd(), libc::POLLIN)?;
    match fault {
        Some(AbandonFault::WaitpidEintr) => {
            return Err(CohortError::Io(
                "waitpid(abandon controller)",
                io::Error::from_raw_os_error(libc::EINTR),
            ));
        }
        Some(AbandonFault::WaitpidZero) => {
            return Err(CohortError::Protocol("abandon waitpid pending"));
        }
        Some(AbandonFault::WaitpidError) => {
            return Err(CohortError::Io(
                "waitpid(abandon controller)",
                io::Error::from_raw_os_error(libc::ECHILD),
            ));
        }
        _ => {}
    }
    match rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::NOHANG) {
        Ok(Some(_)) => Ok(()),
        Ok(None) => Err(CohortError::Protocol("abandon waitpid pending")),
        Err(error) => Err(CohortError::Io(
            "waitpid(abandon controller)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )),
    }
}

fn wait_worker(worker: ProcessWorker) -> Result<(), CohortError> {
    let forced = wait_for_io_timeout(
        worker.pidfd.as_raw_fd(),
        libc::POLLIN,
        CONTROLLER_EXIT_TIMEOUT_MS,
    )
    .is_err();
    let pid =
        rustix::process::Pid::from_raw(worker.pid).ok_or(CohortError::Protocol("worker pid"))?;
    if forced {
        let _ = rustix::process::pidfd_send_signal(&worker.pidfd, rustix::process::Signal::KILL);
    }
    loop {
        match rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::empty()) {
            Ok(Some((_pid, status))) => {
                if !forced && status.exit_status() == Some(0) {
                    return Ok(());
                }
                return Err(CohortError::Process);
            }
            Ok(None) => continue,
            Err(rustix::io::Errno::INTR) => continue,
            Err(error) => {
                return Err(CohortError::Io(
                    "waitpid(controller)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                ))
            }
        }
    }
}

struct TransactionSidecar {
    parent: OwnedFd,
    name: Vec<u8>,
    identity: FileIdentity,
    directory: OwnedFd,
    lease: OwnedFd,
    lease_identity: FileIdentity,
    armed: bool,
    removed: bool,
}

impl TransactionSidecar {
    fn arm(&mut self) {
        self.armed = true;
    }

    fn cleanup(&mut self) -> Result<(), CohortError> {
        if self.removed {
            return Ok(());
        }
        if identity(self.directory.as_raw_fd())? != self.identity
            || named_identity(self.parent.as_fd(), &self.name)? != Some(self.identity)
            || identity(self.lease.as_raw_fd())? != self.lease_identity
            || named_identity(self.directory.as_fd(), b"owner.lock")? != Some(self.lease_identity)
        {
            return Err(CohortError::Identity("transaction sidecar cleanup"));
        }
        let lock_name = component(b"owner.lock")?;
        rustix::fs::unlinkat(
            &self.directory,
            lock_name.as_c_str(),
            rustix::fs::AtFlags::empty(),
        )
        .map_err(|error| {
            CohortError::Io(
                "unlink transaction sidecar lease",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;
        let name = component(&self.name)?;
        rustix::fs::unlinkat(
            &self.parent,
            name.as_c_str(),
            rustix::fs::AtFlags::REMOVEDIR,
        )
        .map_err(|error| {
            CohortError::Io(
                "remove transaction sidecar",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;
        self.removed = true;
        Ok(())
    }
}

impl Drop for TransactionSidecar {
    fn drop(&mut self) {
        if !self.removed && (!self.armed || cfg!(test)) {
            let _ = self.cleanup();
        }
    }
}

fn acquire_transaction_sidecar(
    prefix: RawFd,
    expected_prefix: FileIdentity,
) -> Result<TransactionSidecar, CohortError> {
    if identity(prefix)? != expected_prefix {
        return Err(CohortError::Identity("transaction sidecar prefix binding"));
    }
    let retained_prefix = crate::inherited_fd::duplicate_cloexec(prefix, 3)
        .map_err(|error| CohortError::Io("duplicate transaction prefix", error))?;
    let parent = rustix::fs::openat(
        &retained_prefix,
        c"..",
        rustix::fs::OFlags::PATH
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|error| {
        CohortError::Io(
            "open prefix parent for transaction sidecar",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    let sidecar_name = CString::new(format!(
        ".darling-lifecycle-{:x}-{:x}",
        expected_prefix.device, expected_prefix.inode
    ))
    .map_err(|_| CohortError::Protocol("transaction sidecar name"))?;
    let created = match rustix::fs::mkdirat(
        &parent,
        sidecar_name.as_c_str(),
        rustix::fs::Mode::from_raw_mode(0o700),
    ) {
        Ok(()) => true,
        Err(rustix::io::Errno::EXIST) => false,
        Err(error) => {
            return Err(CohortError::Io(
                "mkdir transaction sidecar",
                io::Error::from_raw_os_error(error.raw_os_error()),
            ))
        }
    };
    if created {
        rustix::fs::chmodat(
            &parent,
            sidecar_name.as_c_str(),
            rustix::fs::Mode::from_raw_mode(0o700),
            rustix::fs::AtFlags::empty(),
        )
        .map_err(|error| {
            CohortError::Io(
                "normalize transaction sidecar mode",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;
    }
    let directory = rustix::fs::openat(
        &parent,
        sidecar_name.as_c_str(),
        rustix::fs::OFlags::PATH
            | rustix::fs::OFlags::DIRECTORY
            | rustix::fs::OFlags::NOFOLLOW
            | rustix::fs::OFlags::CLOEXEC,
        rustix::fs::Mode::empty(),
    )
    .map_err(|error| {
        CohortError::Io(
            "open transaction sidecar",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    let observed = identity(directory.as_raw_fd())?;
    if file_type(observed) != libc::S_IFDIR
        || observed.uid != current_uid()
        || observed.mode & 0o777 != 0o700
    {
        return Err(CohortError::Identity("transaction sidecar"));
    }
    let lease = openat(
        directory.as_fd(),
        b"owner.lock",
        libc::O_RDWR | libc::O_CREAT | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        0o600,
    )?;
    rustix::fs::flock(&lease, rustix::fs::FlockOperation::NonBlockingLockExclusive)
        .map_err(|_| CohortError::LockBusy)?;
    let lease_identity = identity(lease.as_raw_fd())?;
    Ok(TransactionSidecar {
        parent,
        name: sidecar_name.to_bytes().to_vec(),
        identity: observed,
        directory,
        lease,
        lease_identity,
        armed: false,
        removed: false,
    })
}

pub struct CohortController {
    shutdown: OwnedFd,
    dserver_log: Option<OwnedFd>,
    #[cfg(test)]
    thread: Option<JoinHandle<ServerLoopResult>>,
    #[cfg(not(test))]
    process: Option<ProcessWorker>,
    control_name: Vec<u8>,
    nonce: [u8; NONCE_BYTES],
    guest_namespace: Option<GuestNamespaceAuthority>,
    guest_transactions: Mutex<Option<GuestNamespaceTransactionService>>,
    runtime_lower_binding: Mutex<Option<RuntimeLowerBinding>>,
    preinit_var_run_recovery: Mutex<bool>,
    preinit_var_parent: Option<OwnedFd>,
    prefix_state_v3: bool,
    deployment_prefix: OwnedFd,
    prefix_generation: u64,
    transaction_sidecar: TransactionSidecar,
    cleanup_phase: CleanupPhase,
    forensic_preserve_requested: bool,
    forensic_preserve_acknowledged: bool,
    #[cfg(test)]
    lose_cleanup_ack_after_receive: bool,
    #[cfg(test)]
    abandon_fault: Option<AbandonFault>,
    #[cfg(test)]
    control_test_path: std::path::PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum CleanupPhase {
    Active,
    Draining,
    CleanupRequested,
    CleanupCommitted,
    RecoveryPending,
    Abandoning,
    Finished,
}

impl CohortController {
    fn prepare_user_home(
        &mut self,
        plan: crate::preinit_user_home::UserHomePlan,
    ) -> Result<(), CohortError> {
        let authority = self.guest_namespace.as_ref().ok_or(CohortError::Protocol(
            "guest namespace authority unavailable",
        ))?;
        if !authority.is_active() || self.cleanup_phase != CleanupPhase::Active {
            return Err(CohortError::Protocol("guest home admission closed"));
        }
        let prefix = crate::inherited_fd::duplicate_cloexec(authority.prefix_fd(), 3)
            .map_err(|error| CohortError::Io("duplicate guest home prefix", error))?;
        crate::preinit_user_home::prepare(prefix.as_fd(), &plan)
            .map_err(|_| CohortError::Protocol("persistent guest home preparation refused"))
    }

    fn prepare_var_run(&mut self) -> Result<crate::preinit_var_run::VarRunOutcome, CohortError> {
        let (prefix, session_generation) = {
            let authority = self.guest_namespace.as_ref().ok_or(CohortError::Protocol(
                "guest namespace authority unavailable",
            ))?;
            (authority.prefix_fd(), authority.generation())
        };
        if !self.prefix_state_v3 {
            return Err(CohortError::Protocol(
                "generation var/run requires runtime prefix state v3",
            ));
        }
        if self.preinit_var_parent.is_none() {
            self.preinit_var_parent = Some(
                crate::preinit_var_run::acquire_var_parent(prefix).map_err(|error| {
                    eprintln!("generation var/run parent acquisition refused: {error}");
                    CohortError::Protocol("generation var/run parent acquisition refused")
                })?,
            );
        }
        let parent = self
            .preinit_var_parent
            .as_ref()
            .ok_or(CohortError::Protocol(
                "generation var/run parent unavailable",
            ))?;
        match crate::preinit_var_run::prepare_retained(
            prefix,
            parent.as_raw_fd(),
            session_generation,
        ) {
            Ok(outcome) => Ok(outcome),
            Err(error) => {
                eprintln!("generation var/run authority error: {error}");
                if error.recovery_required() {
                    *self
                        .preinit_var_run_recovery
                        .lock()
                        .unwrap_or_else(|p| p.into_inner()) = true;
                    if let Some(authority) = self.guest_namespace.as_mut() {
                        let _ = authority.revoke();
                    }
                    self.revoke_guest_transactions();
                }
                Err(CohortError::Protocol(
                    "generation var/run preparation refused",
                ))
            }
        }
    }

    fn preinit_var_run_recovery_pending(&self) -> bool {
        *self
            .preinit_var_run_recovery
            .lock()
            .unwrap_or_else(|p| p.into_inner())
    }
    fn configure_guest_transactions(&self) -> Result<(), CohortError> {
        let authority = self.guest_namespace.as_ref().ok_or(CohortError::Protocol(
            "guest namespace authority unavailable",
        ))?;
        let mut binding_slot = self
            .runtime_lower_binding
            .lock()
            .map_err(|_| CohortError::Protocol("runtime lower binding mutex poisoned"))?;
        if binding_slot.is_none() {
            *binding_slot = Some(
                RuntimeLowerBinding::acquire(
                    authority.prefix_fd(),
                    identity(authority.prefix_fd())?,
                    self.deployment_prefix.as_raw_fd(),
                    identity(self.deployment_prefix.as_raw_fd())?,
                    self.prefix_generation,
                )
                .map_err(|_| CohortError::Identity("runtime lower deployment binding"))?,
            );
        }
        let binding = binding_slot
            .as_ref()
            .ok_or(CohortError::Protocol("runtime lower binding unavailable"))?;
        binding
            .revalidate(authority.prefix_fd(), self.deployment_prefix.as_raw_fd())
            .map_err(|_| CohortError::Identity("runtime lower deployment binding"))?;
        let service = GuestNamespaceTransactionService::from_retained_roots(
            authority.prefix_fd(),
            binding.lower_fd(),
            self.transaction_sidecar.directory.as_raw_fd(),
            authority.generation(),
        )
        .map_err(|_| CohortError::Protocol("guest transaction authority"))?;
        let mut slot = self
            .guest_transactions
            .lock()
            .map_err(|_| CohortError::Protocol("guest transaction mutex poisoned"))?;
        if slot.is_some() {
            return Err(CohortError::Protocol(
                "guest transaction authority already configured",
            ));
        }
        *slot = Some(service);
        Ok(())
    }

    fn execute_guest_transaction(
        &self,
        request: GuestTransactionRequest,
    ) -> Result<GuestTransactionOutcome, CohortError> {
        let binding_slot = self
            .runtime_lower_binding
            .lock()
            .map_err(|_| CohortError::Protocol("runtime lower binding mutex poisoned"))?;
        let binding = binding_slot
            .as_ref()
            .ok_or(CohortError::Protocol("runtime lower binding unavailable"))?;
        let authority = self.guest_namespace.as_ref().ok_or(CohortError::Protocol(
            "guest namespace authority unavailable",
        ))?;
        binding
            .revalidate(authority.prefix_fd(), self.deployment_prefix.as_raw_fd())
            .map_err(|_| CohortError::Identity("runtime lower deployment binding"))?;
        let mut slot = self
            .guest_transactions
            .lock()
            .map_err(|_| CohortError::Protocol("guest transaction mutex poisoned"))?;
        slot.as_mut()
            .ok_or(CohortError::Protocol(
                "guest transaction authority not configured",
            ))?
            .execute(request)
            .map_err(|_| CohortError::Protocol("guest transaction refused"))
    }

    fn duplicate_vchroot_directory(&self) -> Result<OwnedFd, CohortError> {
        let binding_slot = self
            .runtime_lower_binding
            .lock()
            .map_err(|_| CohortError::Protocol("runtime lower binding mutex poisoned"))?;
        let binding = binding_slot
            .as_ref()
            .ok_or(CohortError::Protocol("runtime lower binding unavailable"))?;
        let authority = self.guest_namespace.as_ref().ok_or(CohortError::Protocol(
            "guest namespace authority unavailable",
        ))?;
        binding
            .revalidate(authority.prefix_fd(), self.deployment_prefix.as_raw_fd())
            .map_err(|_| CohortError::Identity("runtime lower deployment binding"))?;
        crate::inherited_fd::duplicate_cloexec(authority.prefix_fd(), 3)
            .map_err(|error| CohortError::Io("duplicate(vchroot directory)", error))
    }

    fn revoke_guest_transactions(&self) {
        let mut slot = self
            .guest_transactions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if let Some(service) = slot.as_mut() {
            service.revoke();
        }
    }

    fn guest_transaction_recovery_pending(&self) -> bool {
        let slot = self
            .guest_transactions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        slot.as_ref()
            .is_some_and(GuestNamespaceTransactionService::has_outstanding_recovery)
    }

    fn cleanup_guest_transaction_sidecar(&mut self) -> Result<(), CohortError> {
        let mut slot = self
            .guest_transactions
            .lock()
            .map_err(|_| CohortError::Protocol("guest transaction mutex poisoned"))?;
        if let Some(service) = slot.as_mut() {
            service
                .prepare_clean_sidecar()
                .map_err(|_| CohortError::Protocol("guest transaction sidecar not clean"))?;
        }
        *slot = None;
        self.transaction_sidecar.cleanup()
    }

    #[cfg(test)]
    fn discard_forensic_transaction_sidecar_for_contract(&mut self) -> Result<(), CohortError> {
        let mut slot = self
            .guest_transactions
            .lock()
            .map_err(|_| CohortError::Protocol("guest transaction mutex poisoned"))?;
        if let Some(service) = slot.as_mut() {
            service
                .discard_forensic_sidecar_for_contract()
                .map_err(|_| CohortError::Protocol("discard contract transaction sidecar"))?;
        }
        *slot = None;
        self.transaction_sidecar.cleanup()
    }

    pub fn start(prefix: &Path, init_pid: libc::pid_t) -> Result<(Self, OwnedFd), CohortError> {
        let authority = SessionAuthority::acquire(prefix, init_pid)?;
        let deployment_prefix = duplicate(authority.prefix.as_fd())?;
        Self::start_with_authority(authority, deployment_prefix, Some(prefix), false)
    }

    fn start_from_fd(
        prefix_fd: RawFd,
        deployment_prefix_fd: RawFd,
        prefix_argument: &[u8],
        init_pid: libc::pid_t,
    ) -> Result<(Self, OwnedFd), CohortError> {
        let authority = SessionAuthority::acquire_from_fd(prefix_fd, prefix_argument, init_pid)?;
        let deployment_prefix = crate::inherited_fd::duplicate_cloexec(deployment_prefix_fd, 3)
            .map_err(|error| CohortError::Io("fcntl(F_DUPFD_CLOEXEC)", error))?;
        #[cfg(test)]
        let test_prefix = Some(Path::new(std::ffi::OsStr::from_bytes(prefix_argument)));
        #[cfg(not(test))]
        let test_prefix = None;
        Self::start_with_authority(authority, deployment_prefix, test_prefix, false)
    }

    fn start_with_authority(
        mut authority: SessionAuthority,
        deployment_prefix: OwnedFd,
        test_prefix: Option<&Path>,
        allow_test_peer: bool,
    ) -> Result<(Self, OwnedFd), CohortError> {
        let prefix_generation = authority.prefix_state.generation;
        let prefix_state_v3 = authority.prefix_state.name == PREFIX_STATE_V3_NAME;
        #[cfg(test)]
        {
            authority.allow_test_peer = allow_test_peer;
        }
        #[cfg(not(test))]
        let _ = (allow_test_peer, test_prefix);
        let darlingserver = authority.publish(CohortEndpoint::DarlingServer)?;
        let dserver_log = authority.publish_log()?;
        let nonce = random_nonce()?;
        let generation = u64::from_ne_bytes(nonce[..8].try_into().expect("nonce width")) | 1;
        let guest_namespace = Some(
            GuestNamespaceAuthority::issue_from_locked_fds(
                authority.prefix.as_raw_fd(),
                authority.lock.as_raw_fd(),
                generation,
            )
            .map_err(|_| CohortError::Protocol("guest namespace authority"))?,
        );
        let mut transaction_sidecar =
            acquire_transaction_sidecar(authority.prefix.as_raw_fd(), authority.prefix_identity)?;
        #[cfg(not(test))]
        let initial_binding = Some(
            RuntimeLowerBinding::acquire(
                authority.prefix.as_raw_fd(),
                authority.prefix_identity,
                deployment_prefix.as_raw_fd(),
                identity(deployment_prefix.as_raw_fd())?,
                prefix_generation,
            )
            .map_err(|_| CohortError::Identity("runtime lower deployment binding"))?,
        );
        #[cfg(test)]
        let initial_binding: Option<RuntimeLowerBinding> = None;
        let name = CONTROL_GUEST_PATH.to_vec();
        let listener = authority.publish(CohortEndpoint::Control)?;
        let (shutdown, child_control) = rustix::net::socketpair(
            rustix::net::AddressFamily::UNIX,
            rustix::net::SocketType::SEQPACKET,
            rustix::net::SocketFlags::CLOEXEC,
            None,
        )
        .map_err(|error| {
            CohortError::Io(
                "socketpair(controller command)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?;

        #[cfg(test)]
        let controller = {
            let thread = thread::Builder::new()
                .name("darling-lifecycle-cohort".to_string())
                .spawn(move || server_loop(authority, listener, child_control, None, nonce))
                .map_err(|error| CohortError::Io("spawn(controller)", error))?;
            transaction_sidecar.arm();
            Self {
                shutdown,
                dserver_log: Some(dserver_log),
                thread: Some(thread),
                control_name: name,
                nonce,
                guest_namespace,
                guest_transactions: Mutex::new(None),
                runtime_lower_binding: Mutex::new(initial_binding),
                preinit_var_run_recovery: Mutex::new(false),
                preinit_var_parent: None,
                prefix_state_v3,
                deployment_prefix,
                prefix_generation,
                transaction_sidecar,
                cleanup_phase: CleanupPhase::Active,
                forensic_preserve_requested: false,
                forensic_preserve_acknowledged: false,
                lose_cleanup_ack_after_receive: false,
                abandon_fault: None,
                #[cfg(test)]
                control_test_path: test_prefix
                    .ok_or(CohortError::Protocol("test prefix path"))?
                    .join(std::ffi::OsStr::from_bytes(CONTROL_NAME)),
            }
        };

        #[cfg(not(test))]
        let controller = {
            let worker_binding = initial_binding
                .as_ref()
                .ok_or(CohortError::Protocol("controller worker binding"))?;
            worker_binding
                .revalidate(authority.prefix.as_raw_fd(), deployment_prefix.as_raw_fd())
                .map_err(|_| CohortError::Identity("controller worker replacement"))?;
            let worker_fd = worker_binding.worker_fd();
            let process = cohort_ffi::spawn_controller_process(
                authority,
                listener,
                child_control,
                worker_fd,
                nonce,
            )?;
            transaction_sidecar.arm();
            Self {
                shutdown,
                dserver_log: Some(dserver_log),
                process: Some(process),
                control_name: name,
                nonce,
                guest_namespace,
                guest_transactions: Mutex::new(None),
                runtime_lower_binding: Mutex::new(initial_binding),
                preinit_var_run_recovery: Mutex::new(false),
                preinit_var_parent: None,
                prefix_state_v3,
                deployment_prefix,
                prefix_generation,
                transaction_sidecar,
                cleanup_phase: CleanupPhase::Active,
                forensic_preserve_requested: false,
                forensic_preserve_acknowledged: false,
            }
        };

        Ok((controller, darlingserver))
    }

    #[cfg(test)]
    fn start_for_test(
        prefix: &Path,
        _init_pid: libc::pid_t,
    ) -> Result<(Self, OwnedFd), CohortError> {
        // Production publication is now identity-bound and therefore cannot
        // use the historical synthetic PID supplied by older unit fixtures.
        let authority = SessionAuthority::acquire(prefix, std::process::id() as libc::pid_t)?;
        let deployment_prefix = duplicate(authority.prefix.as_fd())?;
        Self::start_with_authority(authority, deployment_prefix, Some(prefix), true)
    }

    pub fn control_name(&self) -> &[u8] {
        &self.control_name
    }

    pub fn nonce_hex(&self) -> [u8; NONCE_HEX_BYTES] {
        nonce_hex(&self.nonce)
    }

    pub fn send_guest_namespace_bootstrap(&self, socket: RawFd) -> Result<(), CohortError> {
        let authority = self.guest_namespace.as_ref().ok_or(CohortError::Protocol(
            "guest namespace authority unavailable",
        ))?;
        let binding_slot = self
            .runtime_lower_binding
            .lock()
            .map_err(|_| CohortError::Protocol("runtime lower binding mutex poisoned"))?;
        let binding = binding_slot
            .as_ref()
            .ok_or(CohortError::Protocol("runtime lower binding unavailable"))?;
        binding
            .revalidate(authority.prefix_fd(), self.deployment_prefix.as_raw_fd())
            .map_err(|_| CohortError::Identity("runtime lower deployment binding"))?;
        authority
            .send_bootstrap_with_lower(socket, binding.lower_fd())
            .map_err(|_| CohortError::Protocol("guest namespace bootstrap"))
    }

    fn take_dserver_log(&mut self) -> Result<OwnedFd, CohortError> {
        self.dserver_log.take().ok_or(CohortError::Protocol(
            "Darlingserver log already transferred",
        ))
    }

    #[must_use = "drain-pending returns the owning controller and must be recovered"]
    pub fn finish(mut self) -> Result<(), CohortFinishError> {
        if self.cleanup_phase == CleanupPhase::CleanupCommitted {
            return self
                .finish_after_cleanup_commit()
                .map_err(CohortFinishError::Terminal);
        }
        if self.cleanup_phase == CleanupPhase::RecoveryPending {
            return Err(CohortFinishError::RecoveryPending {
                controller: Box::new(self),
            });
        }
        if self.cleanup_phase == CleanupPhase::Abandoning {
            return Err(CohortFinishError::CleanupRequestPending {
                controller: Box::new(self),
                source: CohortError::Protocol("normal finish forbidden after abandon request"),
            });
        }
        self.cleanup_phase = CleanupPhase::Draining;
        let Some(guest_namespace) = self.guest_namespace.as_mut() else {
            return Err(CohortFinishError::Terminal(CohortError::Protocol(
                "guest namespace authority unavailable",
            )));
        };
        if let Err(source) = guest_namespace.revoke() {
            return Err(CohortFinishError::DrainPending {
                controller: Box::new(self),
                source,
            });
        }
        self.revoke_guest_transactions();
        if self.guest_transaction_recovery_pending() {
            self.cleanup_phase = CleanupPhase::RecoveryPending;
            return Err(CohortFinishError::RecoveryPending {
                controller: Box::new(self),
            });
        }
        if let Err(source) = self.request_cleanup() {
            return if self.cleanup_phase == CleanupPhase::CleanupCommitted {
                Err(CohortFinishError::CleanupPending {
                    controller: Box::new(self),
                    source,
                })
            } else {
                Err(CohortFinishError::CleanupRequestPending {
                    controller: Box::new(self),
                    source,
                })
            };
        }
        self.finish_after_cleanup_commit()
            .map_err(CohortFinishError::Terminal)
    }

    fn send_controller_command(&self, command: u8) -> Result<(), CohortError> {
        let sent = rustix::net::send(&self.shutdown, &[command], rustix::net::SendFlags::NOSIGNAL)
            .map_err(|error| {
                CohortError::Io(
                    "send(controller command)",
                    io::Error::from_raw_os_error(error.raw_os_error()),
                )
            })?;
        if sent != 1 {
            return Err(CohortError::Protocol("controller command size"));
        }
        Ok(())
    }

    fn receive_controller_ack(&self) -> Result<(), CohortError> {
        wait_for_io(self.shutdown.as_raw_fd(), libc::POLLIN)?;
        let mut acknowledged = [0u8; 2];
        let received = rustix::net::recv(
            &self.shutdown,
            &mut acknowledged,
            rustix::net::RecvFlags::empty(),
        )
        .map_err(|error| {
            CohortError::Io(
                "recv(controller acknowledgement)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            )
        })?
        .0 as isize;
        if received != 1 || acknowledged[0] != CONTROLLER_COMMAND_ACK {
            return Err(CohortError::Protocol("controller command acknowledgement"));
        }
        Ok(())
    }

    fn request_cleanup(&mut self) -> Result<(), CohortError> {
        if self.forensic_preserve_requested
            || !matches!(
                self.cleanup_phase,
                CleanupPhase::Draining | CleanupPhase::CleanupRequested
            )
        {
            return Err(CohortError::Protocol(
                "cleanup forbidden after forensic preserve request",
            ));
        }
        self.cleanup_phase = CleanupPhase::CleanupRequested;
        if let Err(error) = self.send_controller_command(CONTROLLER_COMMAND_CLEANUP) {
            self.cleanup_phase = CleanupPhase::Draining;
            return Err(error);
        }
        // The successful packet send is the irreversible cleanup commit.  No
        // later ACK/exit/reap failure may transition to preserve/abandon.
        self.cleanup_phase = CleanupPhase::CleanupCommitted;
        self.receive_controller_ack()?;
        #[cfg(test)]
        if self.lose_cleanup_ack_after_receive {
            self.lose_cleanup_ack_after_receive = false;
            return Err(CohortError::Protocol("fault(cleanup ACK lost)"));
        }
        Ok(())
    }

    fn finish_after_cleanup_commit(mut self) -> Result<(), CohortError> {
        #[cfg(test)]
        {
            let (mut authority, loop_result, forensic_preserve) = self
                .thread
                .take()
                .ok_or(CohortError::Thread)?
                .join()
                .map_err(|_| CohortError::Thread)?;
            if forensic_preserve {
                authority.disarm_drop_cleanup();
                return Err(CohortError::Protocol(
                    "unexpected forensic preserve on finish",
                ));
            }
            let cleanup_result = authority.cleanup_all();
            cleanup_result?;
            loop_result?;
            self.cleanup_guest_transaction_sidecar()?;
            self.cleanup_phase = CleanupPhase::Finished;
            Ok(())
        }
        #[cfg(not(test))]
        {
            wait_worker(self.process.take().ok_or(CohortError::Process)?)?;
            self.cleanup_guest_transaction_sidecar()?;
            self.cleanup_phase = CleanupPhase::Finished;
            Ok(())
        }
    }

    fn request_forensic_preserve(&mut self) -> Result<(), CohortError> {
        if self.forensic_preserve_acknowledged {
            return Ok(());
        }
        if matches!(
            self.cleanup_phase,
            CleanupPhase::CleanupRequested
                | CleanupPhase::CleanupCommitted
                | CleanupPhase::Finished
        ) {
            return Err(CohortError::Protocol(
                "forensic preserve forbidden after cleanup request",
            ));
        }
        self.cleanup_phase = CleanupPhase::Abandoning;
        self.forensic_preserve_requested = true;
        self.send_controller_command(CONTROLLER_COMMAND_PRESERVE)?;
        self.receive_controller_ack()?;
        self.forensic_preserve_acknowledged = true;
        Ok(())
    }

    #[doc(hidden)]
    pub fn acknowledge_forensic_preserve_for_contract(&mut self) -> Result<(), CohortError> {
        self.request_forensic_preserve()
    }

    fn abandon_after_revocation(&mut self) -> Result<(), CohortError> {
        self.request_forensic_preserve()?;
        #[cfg(test)]
        {
            if let Some(fault) = self.abandon_fault.take() {
                return Err(CohortError::Protocol(match fault {
                    AbandonFault::KillError => "fault(abandon kill)",
                    AbandonFault::PidfdTimeout => "fault(abandon pidfd timeout)",
                    AbandonFault::WaitpidEintr => "fault(abandon waitpid EINTR)",
                    AbandonFault::WaitpidZero => "fault(abandon waitpid zero)",
                    AbandonFault::WaitpidError => "fault(abandon waitpid error)",
                }));
            }
            let (mut authority, loop_result, forensic_preserve) = self
                .thread
                .take()
                .ok_or(CohortError::Thread)?
                .join()
                .map_err(|_| CohortError::Thread)?;
            if !forensic_preserve {
                return Err(CohortError::Protocol("forensic preserve not acknowledged"));
            }
            authority.disarm_drop_cleanup();
            loop_result
        }
        #[cfg(not(test))]
        {
            let worker = self.process.as_ref().ok_or(CohortError::Process)?;
            abandon_process_worker(worker, None)?;
            self.process.take();
            Ok(())
        }
    }
}

impl Drop for CohortController {
    fn drop(&mut self) {}
}

#[must_use = "a pending controller retains worker or recovery ownership"]
pub enum CohortFinishError {
    DrainPending {
        controller: Box<CohortController>,
        source: crate::guest_namespace_authority::AuthorityError,
    },
    CleanupRequestPending {
        controller: Box<CohortController>,
        source: CohortError,
    },
    CleanupPending {
        controller: Box<CohortController>,
        source: CohortError,
    },
    RecoveryPending {
        controller: Box<CohortController>,
    },
    Terminal(CohortError),
}

impl std::fmt::Debug for CohortFinishError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::DrainPending { source, .. } => {
                formatter.debug_tuple("DrainPending").field(source).finish()
            }
            Self::CleanupRequestPending { source, .. } => formatter
                .debug_tuple("CleanupRequestPending")
                .field(source)
                .finish(),
            Self::CleanupPending { source, .. } => formatter
                .debug_tuple("CleanupPending")
                .field(source)
                .finish(),
            Self::RecoveryPending { .. } => formatter.write_str("RecoveryPending"),
            Self::Terminal(error) => formatter.debug_tuple("Terminal").field(error).finish(),
        }
    }
}

const GUEST_TRANSACTION_PATH_CAPACITY: usize = 1024;

#[repr(C)]
pub struct GuestTransactionWireRequest {
    transaction_id: [u8; 16],
    operation: u32,
    flags: i32,
    mode: u32,
    source_length: u16,
    destination_length: u16,
    source: [u8; GUEST_TRANSACTION_PATH_CAPACITY],
    destination: [u8; GUEST_TRANSACTION_PATH_CAPACITY],
}

#[repr(C)]
pub struct GuestTransactionWireResult {
    result: i32,
    disposition: u32,
    device: u64,
    inode: u64,
    created_fd: i32,
    reserved: u32,
}

#[cfg(test)]
#[path = "cohort_test_support.rs"]
#[allow(unsafe_code)]
mod tests;
