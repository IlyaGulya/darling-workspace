//! Rust-owned routing boundary for the first Rootless namespace-writer cohort.
//!
//! The controller retains the prefix directory and the exact
//! `.lifecycle.lock` open-file description for its whole lifetime.  Product
//! processes never receive pathname mutation authority: they request one of
//! the finite endpoint kinds and receive only the already-bound listener via
//! `SCM_RIGHTS`.  The v1 threat model is deliberately cooperative; writers
//! outside this cohort keep global routing disabled.

use crate::guest_namespace_authority::GuestNamespaceAuthority;
use crate::FileIdentity;
use std::collections::BTreeMap;
use std::ffi::{CStr, CString};
use std::io;
use std::mem::{size_of, MaybeUninit};
use std::os::fd::{AsRawFd, FromRawFd, IntoRawFd, OwnedFd, RawFd};
use std::os::raw::{c_char, c_int};
use std::os::unix::ffi::OsStrExt;
use std::path::Path;
use std::ptr;
use std::thread;
#[cfg(test)]
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

const PREFIX_STATE_NAME: &[u8] = b".darling-prefix-state-v2";
const PREFIX_STATE_HEADER: &str = "DARLING_PREFIX_STATE_V2";
const PREFIX_STATE_PROVENANCE: &str = "darling-runtime-prefix-lifecycle-v2";
const PREFIX_STATE_MAX_BYTES: usize = 1024;
const LOCK_NAME: &[u8] = b".lifecycle.lock";
const INIT_PID_NAME: &[u8] = b".init.pid";
const PROTOCOL_MAGIC: [u8; 8] = *b"DLCOHR1\0";
const PROTOCOL_VERSION: u16 = 1;
const OPERATION_PUBLISH: u16 = 1;
const OPERATION_RETIRE: u16 = 2;
const OPERATION_COMMIT: u16 = 3;
const OPERATION_ABORT: u16 = 4;
const CONTROL_NAME_CAPACITY: usize = 80;
const NONCE_BYTES: usize = 32;
const NONCE_HEX_BYTES: usize = NONCE_BYTES * 2;
const DYNAMIC_PATH_CAPACITY: usize = 96;
const DYNAMIC_DIRECTORY_ATTEMPTS: usize = 16;
const LOCK_TIMEOUT: Duration = Duration::from_millis(250);
const REQUEST_TIMEOUT_MS: c_int = 250;
const ABANDON_PENDING_STATUS: c_int = 3;
const CLEANUP_PENDING_STATUS: c_int = 4;
const CONTROLLER_COMMAND_CLEANUP: u8 = 1;
const CONTROLLER_COMMAND_PRESERVE: u8 = 2;
const CONTROLLER_COMMAND_ACK: u8 = 0x7f;
#[cfg(not(test))]
const CONTROLLER_EXIT_TIMEOUT_MS: c_int = 1_000;
const MAX_REJECTED_REQUESTS_PER_SLICE: usize = 128;
const SO_PEERPIDFD: c_int = 77;
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

fn duplicate(fd: RawFd, cloexec: bool) -> Result<OwnedFd, CohortError> {
    let command = if cloexec {
        libc::F_DUPFD_CLOEXEC
    } else {
        libc::F_DUPFD
    };
    // SAFETY: fcntl returns a new descriptor on success.
    let result = unsafe { libc::fcntl(fd, command, 3) };
    if result < 0 {
        return Err(io_error("fcntl(F_DUPFD)"));
    }
    // SAFETY: result is a fresh descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(result) })
}

fn openat(
    parent: RawFd,
    name: &[u8],
    flags: c_int,
    mode: libc::mode_t,
) -> Result<OwnedFd, CohortError> {
    let name = component(name)?;
    // SAFETY: name is NUL terminated and the returned fd is uniquely owned.
    let result = unsafe { libc::openat(parent, name.as_ptr(), flags, mode) };
    if result < 0 {
        return Err(io_error("openat"));
    }
    // SAFETY: result is a fresh descriptor.
    Ok(unsafe { OwnedFd::from_raw_fd(result) })
}

fn identity(fd: RawFd) -> Result<FileIdentity, CohortError> {
    FileIdentity::from_fd(fd).map_err(|_| io_error("fstat"))
}

fn named_identity(parent: RawFd, name: &[u8]) -> Result<Option<FileIdentity>, CohortError> {
    let name = component(name)?;
    match FileIdentity::from_at(parent, &name) {
        Ok(value) => Ok(Some(value)),
        Err(_) if io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) => Ok(None),
        Err(_) => Err(io_error("fstatat")),
    }
}

fn file_type(value: FileIdentity) -> u32 {
    value.mode & libc::S_IFMT
}

fn current_uid() -> libc::uid_t {
    // SAFETY: geteuid has no preconditions.
    unsafe { libc::geteuid() }
}

fn current_gid() -> libc::gid_t {
    // SAFETY: getegid has no preconditions.
    unsafe { libc::getegid() }
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

fn read_bounded(fd: RawFd, limit: usize) -> Result<Vec<u8>, CohortError> {
    if unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } < 0 {
        return Err(io_error("lseek"));
    }
    let mut output = Vec::with_capacity(limit.min(4096));
    let mut buffer = [0u8; 256];
    loop {
        // SAFETY: buffer is writable for buffer.len() bytes.
        let count = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
        if count < 0 {
            return Err(io_error("read"));
        }
        if count == 0 {
            return Ok(output);
        }
        let count = count as usize;
        if output.len() + count > limit {
            return Err(CohortError::Protocol("bounded file exceeded"));
        }
        output.extend_from_slice(&buffer[..count]);
    }
}

fn write_all(fd: RawFd, mut bytes: &[u8]) -> Result<(), CohortError> {
    while !bytes.is_empty() {
        // SAFETY: bytes is readable for its declared length.
        let count = unsafe { libc::write(fd, bytes.as_ptr().cast(), bytes.len()) };
        if count < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return Err(io_error("write"));
        }
        if count == 0 {
            return Err(CohortError::Protocol("zero-length write"));
        }
        bytes = &bytes[count as usize..];
    }
    Ok(())
}

fn open_prefix(path: &Path) -> Result<OwnedFd, CohortError> {
    if !path.is_absolute() {
        return Err(CohortError::InvalidPrefix);
    }
    let bytes = path.as_os_str().as_bytes();
    let name = CString::new(bytes).map_err(|_| CohortError::InvalidPrefix)?;
    // SAFETY: name is NUL terminated. O_NOFOLLOW rejects a symlink at the
    // retained prefix boundary; every later operation is fd-relative.
    let fd = unsafe {
        libc::open(
            name.as_ptr(),
            libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(io_error("open(prefix)"));
    }
    // SAFETY: fd is fresh.
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let observed = identity(fd.as_raw_fd())?;
    if file_type(observed) != libc::S_IFDIR || observed.uid != current_uid() {
        return Err(CohortError::Identity("prefix"));
    }
    Ok(fd)
}

fn open_directory_chain(prefix: RawFd, parts: &[&[u8]]) -> Result<OwnedFd, CohortError> {
    let mut current = duplicate(prefix, true)?;
    for part in parts {
        current = openat(
            current.as_raw_fd(),
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

fn ensure_directory_chain(prefix: RawFd, parts: &[(&[u8], u32)]) -> Result<OwnedFd, CohortError> {
    let mut current = duplicate(prefix, true)?;
    for (part, mode) in parts {
        let mut created = false;
        let next = match openat(
            current.as_raw_fd(),
            part,
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0,
        ) {
            Ok(value) => value,
            Err(CohortError::Io(_, error)) if error.raw_os_error() == Some(libc::ENOENT) => {
                let name = component(part)?;
                if unsafe { libc::mkdirat(current.as_raw_fd(), name.as_ptr(), *mode) } < 0 {
                    return Err(io_error("mkdirat(endpoint parent)"));
                }
                created = true;
                openat(
                    current.as_raw_fd(),
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
        if created && unsafe { libc::fchmod(next.as_raw_fd(), *mode) } < 0 {
            return Err(io_error("fchmod(endpoint parent)"));
        }
        if identity(next.as_raw_fd())?.mode & 0o7777 != *mode {
            return Err(CohortError::Identity("endpoint parent mode"));
        }
        current = next;
    }
    Ok(current)
}

fn ensure_endpoint_parents(prefix: RawFd) -> Result<(), CohortError> {
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
    parent: RawFd,
    name: &[u8],
    expected: FileIdentity,
) -> Result<(), CohortError> {
    if named_identity(parent, name)?.map(FileIdentity::inode_key) != Some(expected.inode_key()) {
        return Err(CohortError::Identity("new Darlingserver log rollback"));
    }
    let name = component(name)?;
    if unsafe { libc::unlinkat(parent, name.as_ptr(), 0) } < 0 {
        return Err(io_error("unlinkat(new Darlingserver log rollback)"));
    }
    if named_identity(parent, name.as_bytes())?.is_some() {
        return Err(CohortError::Identity(
            "new Darlingserver log rollback result",
        ));
    }
    Ok(())
}

fn acquire_lock(prefix: RawFd) -> Result<(OwnedFd, FileIdentity), CohortError> {
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
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            break;
        }
        let errno = io::Error::last_os_error().raw_os_error();
        if errno != Some(libc::EAGAIN) {
            return Err(io_error("flock(LOCK_EX)"));
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
    if created && unsafe { libc::fchmod(lock.as_raw_fd(), 0o600) } < 0 {
        return Err(io_error("fchmod(new lock)"));
    }
    expected = identity(lock.as_raw_fd())?;
    validate_owned(expected, libc::S_IFREG, Some(0o600))?;
    if named_identity(prefix, LOCK_NAME)? != Some(expected) {
        return Err(CohortError::Identity("split lifecycle lock"));
    }
    Ok((lock, expected))
}

fn parse_prefix_state(content: &[u8], prefix: FileIdentity) -> Result<(), CohortError> {
    let text = std::str::from_utf8(content)
        .map_err(|_| CohortError::Protocol("runtime prefix state encoding"))?;
    let body = text
        .strip_suffix('\n')
        .ok_or(CohortError::Protocol("runtime prefix state terminator"))?;
    let lines = body.split('\n').collect::<Vec<_>>();
    if lines.len() != 9 || lines[0] != PREFIX_STATE_HEADER {
        return Err(CohortError::Protocol("runtime prefix state schema"));
    }
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
    if number(1, "schema_version=")? != 2
        || field(2, "runtime_mode=")? != "rootless-eunion"
        || number(3, "generation=")? == 0
        || number(4, "prefix_device=")? != prefix.device
        || number(5, "prefix_inode=")? != prefix.inode
        || number(6, "owner_uid=")? != u64::from(current_uid())
        || number(7, "owner_gid=")? != u64::from(current_gid())
        || field(8, "provenance=")? != PREFIX_STATE_PROVENANCE
    {
        return Err(CohortError::Identity("runtime prefix state"));
    }
    Ok(())
}

fn acquire_prefix_state(
    prefix: RawFd,
    prefix_identity: FileIdentity,
) -> Result<RetainedState, CohortError> {
    let state = openat(
        prefix,
        PREFIX_STATE_NAME,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0,
    )?;
    let expected = identity(state.as_raw_fd())?;
    validate_owned(expected, libc::S_IFREG, Some(0o600))?;
    let content = read_bounded(state.as_raw_fd(), PREFIX_STATE_MAX_BYTES)?;
    if named_identity(prefix, PREFIX_STATE_NAME)? != Some(expected) {
        return Err(CohortError::Identity("runtime prefix state"));
    }
    parse_prefix_state(&content, prefix_identity)?;
    Ok(RetainedState {
        object: state,
        identity: expected,
        content,
    })
}

fn revalidate_prefix_state(
    prefix: RawFd,
    prefix_identity: FileIdentity,
    state: &RetainedState,
) -> Result<(), CohortError> {
    let content = read_bounded(state.object.as_raw_fd(), PREFIX_STATE_MAX_BYTES)?;
    if identity(state.object.as_raw_fd())? != state.identity
        || named_identity(prefix, PREFIX_STATE_NAME)? != Some(state.identity)
        || content != state.content
    {
        return Err(CohortError::Identity("runtime prefix state"));
    }
    parse_prefix_state(&content, prefix_identity)
}

fn revalidate_lock(prefix: RawFd, lock: RawFd, expected: FileIdentity) -> Result<(), CohortError> {
    if identity(lock)? != expected || named_identity(prefix, LOCK_NAME)? != Some(expected) {
        return Err(CohortError::Identity("split lifecycle lock"));
    }
    Ok(())
}

fn random_nonce() -> Result<[u8; NONCE_BYTES], CohortError> {
    let mut nonce = [0u8; NONCE_BYTES];
    let mut offset = 0usize;
    while offset < nonce.len() {
        // SAFETY: the remaining nonce storage is writable.
        let count = unsafe {
            libc::getrandom(
                nonce.as_mut_ptr().add(offset).cast(),
                nonce.len() - offset,
                0,
            )
        };
        if count < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return Err(io_error("getrandom"));
        }
        if count == 0 {
            return Err(CohortError::Protocol("getrandom returned no data"));
        }
        offset += count as usize;
    }
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
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            pidfd,
            0,
            ptr::null::<libc::siginfo_t>(),
            0,
        )
    };
    if result == 0 {
        return Ok(OwnerProcessState::Alive);
    }
    if io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH) {
        return Ok(OwnerProcessState::Gone);
    }
    Err(io_error("pidfd_send_signal(owner probe)"))
}

fn peer_pidfd(socket: RawFd, expected_pid: libc::pid_t) -> Result<OwnedFd, CohortError> {
    let mut pidfd: c_int = -1;
    let mut length = size_of::<c_int>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            socket,
            libc::SOL_SOCKET,
            SO_PEERPIDFD,
            (&mut pidfd as *mut c_int).cast(),
            &mut length,
        )
    } < 0
        || length as usize != size_of::<c_int>()
        || pidfd < 0
    {
        if pidfd >= 0 {
            unsafe { libc::close(pidfd) };
        }
        return Err(io_error("getsockopt(SO_PEERPIDFD)"));
    }
    // SAFETY: a successful SO_PEERPIDFD returns a new descriptor owned by the
    // caller.
    let pidfd = unsafe { OwnedFd::from_raw_fd(pidfd) };
    if unsafe { libc::fcntl(pidfd.as_raw_fd(), libc::F_SETFD, libc::FD_CLOEXEC) } < 0 {
        return Err(io_error("fcntl(peer pidfd)"));
    }
    let path = CString::new(format!("/proc/self/fdinfo/{}", pidfd.as_raw_fd()))
        .map_err(|_| CohortError::Protocol("peer pidfd path"))?;
    let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(io_error("open(peer pidfd info)"));
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let info = read_bounded(fd.as_raw_fd(), 4096)?;
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
    let path = CString::new(format!("/proc/{pid}/stat"))
        .map_err(|_| CohortError::Protocol("proc stat path"))?;
    let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(io_error("open(proc stat)"));
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let bytes = read_bounded(fd.as_raw_fd(), 4096)?;
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
    let path = CString::new(format!("/proc/{pid}/cmdline"))
        .map_err(|_| CohortError::Protocol("proc cmdline path"))?;
    let fd = unsafe { libc::open(path.as_ptr(), libc::O_RDONLY | libc::O_CLOEXEC) };
    if fd < 0 {
        return Err(io_error("open(proc cmdline)"));
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let bytes = read_bounded(fd.as_raw_fd(), 4096)?;
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
        let prefix = duplicate(prefix_fd, true)?;
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
        let initial_prefix_identity = identity(prefix.as_raw_fd())?;
        let (lock, lock_identity) = acquire_lock(prefix.as_raw_fd())?;
        let prefix_state = acquire_prefix_state(prefix.as_raw_fd(), initial_prefix_identity)?;
        ensure_endpoint_parents(prefix.as_raw_fd())?;
        // Creating a direct child changes a directory's link count. Refresh
        // the retained identity only after the finite bootstrap parents exist;
        // device/inode and the typed prefix-state binding remain unchanged.
        let prefix_identity = identity(prefix.as_raw_fd())?;
        revalidate_prefix_state(prefix.as_raw_fd(), prefix_identity, &prefix_state)?;
        #[cfg(not(test))]
        let _ = prefix_argument;
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
        if identity(self.prefix.as_raw_fd())? != self.prefix_identity {
            return Err(CohortError::Identity("prefix"));
        }
        revalidate_lock(
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.lock_identity,
        )?;
        revalidate_prefix_state(
            self.prefix.as_raw_fd(),
            self.prefix_identity,
            &self.prefix_state,
        )
    }

    fn disarm_drop_cleanup(&mut self) {
        self.cleanup_on_drop = false;
    }

    #[cfg(not(test))]
    fn retained_fds(&self) -> Vec<RawFd> {
        let mut fds = vec![
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.prefix_state.object.as_raw_fd(),
        ];
        if let Some(init_pid) = self.init_pid.as_ref() {
            fds.push(init_pid.object.as_raw_fd());
        }
        for endpoint in self.endpoints.values() {
            fds.extend([endpoint.parent.as_raw_fd(), endpoint.object.as_raw_fd()]);
            if let Some(directory) = endpoint.dynamic_directory.as_ref() {
                fds.push(directory.parent.as_raw_fd());
            }
        }
        for owner in self.endpoint_owners.values() {
            fds.push(owner._process.as_raw_fd());
        }
        if let Some(log) = &self.log {
            fds.extend([log.parent.as_raw_fd(), log.object.as_raw_fd()]);
        }
        fds
    }

    fn publish_log(&mut self) -> Result<OwnedFd, CohortError> {
        self.revalidate()?;
        if self.log.is_some() {
            return Err(CohortError::EndpointExists);
        }
        let parent = open_directory_chain(self.prefix.as_raw_fd(), DSERVER_LOG_PARENT)?;
        let parent_identity = identity(parent.as_raw_fd())?;
        let name = DSERVER_LOG_NAME;
        let existing = named_identity(parent.as_raw_fd(), name)?;
        if let Some(expected) = existing {
            // Validate the named object before asking the kernel for a writer.
            // In particular, opening a FIFO O_WRONLY can block indefinitely.
            validate_owned(expected, libc::S_IFREG, Some(0o644))?;
        }
        let (writer, created) = if existing.is_some() {
            (
                openat(
                    parent.as_raw_fd(),
                    name,
                    libc::O_WRONLY | libc::O_APPEND | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    0,
                )?,
                None,
            )
        } else {
            let writer = openat(
                parent.as_raw_fd(),
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
            if created.is_some() && unsafe { libc::fchmod(writer.as_raw_fd(), 0o644) } < 0 {
                return Err(io_error("fchmod(new Darlingserver log)"));
            }
            #[cfg(test)]
            if created.is_some() && self.fail_log_after_create {
                return Err(CohortError::Protocol("injected post-create log failure"));
            }
            let retained = openat(
                parent.as_raw_fd(),
                name,
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            )?;
            let expected = identity(retained.as_raw_fd())?;
            validate_owned(expected, libc::S_IFREG, Some(0o644))?;
            if identity(writer.as_raw_fd())? != expected
                || named_identity(parent.as_raw_fd(), name)? != Some(expected)
            {
                return Err(CohortError::Identity("Darlingserver log publication"));
            }
            Ok((retained, expected))
        })();
        let (retained, expected) = match publication {
            Ok(publication) => publication,
            Err(error) => {
                if let Some(created) = created {
                    rollback_created_log(parent.as_raw_fd(), name, created)?;
                }
                return Err(error);
            }
        };
        if let Some(created) = created {
            if expected.inode_key() != created.inode_key() {
                rollback_created_log(parent.as_raw_fd(), name, created)?;
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
                || named_identity(log.parent.as_raw_fd(), &log.name)? != Some(log.identity)
            {
                return Err(CohortError::Identity("Darlingserver log replacement"));
            }
        }
        self.log = None;
        Ok(())
    }

    fn publish_init_pid(&mut self, init_pid: libc::pid_t) -> Result<(), CohortError> {
        self.revalidate()?;
        if let Some(existing) = named_identity(self.prefix.as_raw_fd(), INIT_PID_NAME)? {
            validate_owned(existing, libc::S_IFREG, Some(0o600))?;
            let name = component(INIT_PID_NAME)?;
            if unsafe { libc::unlinkat(self.prefix.as_raw_fd(), name.as_ptr(), 0) } < 0 {
                return Err(io_error("unlinkat(stale init pid)"));
            }
        }
        let temporary = format!(".init.pid.lifecycle-{}", unsafe { libc::getpid() });
        let temporary_bytes = temporary.as_bytes();
        let file = openat(
            self.prefix.as_raw_fd(),
            temporary_bytes,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            0o600,
        )?;
        let bytes = format!("{init_pid}\n");
        let mut published_identity = None;
        let result = (|| {
            write_all(file.as_raw_fd(), bytes.as_bytes())?;
            if unsafe { libc::fsync(file.as_raw_fd()) } < 0 {
                return Err(io_error("fsync(init pid)"));
            }
            let retained = openat(
                self.prefix.as_raw_fd(),
                temporary_bytes,
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            )?;
            let retained_identity = identity(retained.as_raw_fd())?;
            validate_owned(retained_identity, libc::S_IFREG, Some(0o600))?;
            published_identity = Some(retained_identity);
            let temporary = component(temporary_bytes)?;
            let destination = component(INIT_PID_NAME)?;
            if unsafe {
                libc::renameat(
                    self.prefix.as_raw_fd(),
                    temporary.as_ptr(),
                    self.prefix.as_raw_fd(),
                    destination.as_ptr(),
                )
            } < 0
            {
                return Err(io_error("renameat(init pid)"));
            }
            if named_identity(self.prefix.as_raw_fd(), INIT_PID_NAME)? != Some(retained_identity) {
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
                unsafe { libc::unlinkat(self.prefix.as_raw_fd(), name.as_ptr(), 0) };
            }
            if let Some(expected) = published_identity {
                if named_identity(self.prefix.as_raw_fd(), INIT_PID_NAME)
                    .ok()
                    .flatten()
                    == Some(expected)
                {
                    if let Ok(name) = component(INIT_PID_NAME) {
                        unsafe { libc::unlinkat(self.prefix.as_raw_fd(), name.as_ptr(), 0) };
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
        let parent = open_directory_chain(self.prefix.as_raw_fd(), PER_USER_PARENT)?;
        for _attempt in 0..DYNAMIC_DIRECTORY_ATTEMPTS {
            let nonce = random_nonce()?;
            let name = format!(
                "launchd-{}-{:02x}{:02x}{:02x}{:02x}",
                peer.pid, nonce[0], nonce[1], nonce[2], nonce[3]
            )
            .into_bytes();
            let component = component(&name)?;
            if unsafe { libc::mkdirat(parent.as_raw_fd(), component.as_ptr(), 0o700) } < 0 {
                if io::Error::last_os_error().raw_os_error() == Some(libc::EEXIST) {
                    continue;
                }
                return Err(io_error("mkdirat(per-user launchd)"));
            }
            #[cfg(test)]
            if self.dynamic_fault == Some(DynamicPublicationFault::AfterDirectoryCreate) {
                unsafe {
                    libc::unlinkat(parent.as_raw_fd(), component.as_ptr(), libc::AT_REMOVEDIR)
                };
                return Err(CohortError::Io(
                    "fault(after dynamic directory create)",
                    io::Error::from_raw_os_error(libc::EINTR),
                ));
            }
            let directory = openat(
                parent.as_raw_fd(),
                &name,
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            );
            let directory = match directory {
                Ok(directory) => directory,
                Err(error) => {
                    unsafe {
                        libc::unlinkat(parent.as_raw_fd(), component.as_ptr(), libc::AT_REMOVEDIR)
                    };
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
                if named_identity(parent.as_raw_fd(), &name)? != Some(directory_identity) {
                    return Err(CohortError::Identity("per-user launchd directory"));
                }
                Ok(directory_identity)
            })();
            let directory_identity = match validated {
                Ok(identity) => identity,
                Err(error) => {
                    unsafe {
                        libc::unlinkat(parent.as_raw_fd(), component.as_ptr(), libc::AT_REMOVEDIR)
                    };
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
                let parent = open_directory_chain(self.prefix.as_raw_fd(), spec.parent)?;
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
        if let Some(stale) = named_identity(parent.as_raw_fd(), spec.name)? {
            validate_owned(stale, libc::S_IFSOCK, Some(spec.mode))?;
            let name = component(spec.name)?;
            if unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), 0) } < 0 {
                return Err(io_error("unlinkat(stale endpoint)"));
            }
        }
        let socket_flags = libc::SOCK_CLOEXEC
            | if spec.nonblocking {
                libc::SOCK_NONBLOCK
            } else {
                0
            };
        let socket = unsafe { libc::socket(libc::AF_UNIX, spec.socket_type | socket_flags, 0) };
        if socket < 0 {
            return Err(io_error("socket(endpoint)"));
        }
        // SAFETY: socket is fresh.
        let socket = unsafe { OwnedFd::from_raw_fd(socket) };
        let path = format!(
            "/proc/self/fd/{}/{}",
            parent.as_raw_fd(),
            String::from_utf8_lossy(spec.name)
        );
        let path = CString::new(path).map_err(|_| CohortError::Protocol("endpoint path"))?;
        let mut address = MaybeUninit::<libc::sockaddr_un>::zeroed();
        let address_ptr = address.as_mut_ptr();
        if path.as_bytes().len() >= unsafe { (*address_ptr).sun_path.len() } {
            return Err(CohortError::Protocol("endpoint path too long"));
        }
        unsafe {
            (*address_ptr).sun_family = libc::AF_UNIX as libc::sa_family_t;
            ptr::copy_nonoverlapping(
                path.as_ptr(),
                (*address_ptr).sun_path.as_mut_ptr(),
                path.as_bytes_with_nul().len(),
            );
        }
        if unsafe {
            libc::bind(
                socket.as_raw_fd(),
                address_ptr.cast(),
                size_of::<libc::sockaddr_un>() as libc::socklen_t,
            )
        } < 0
        {
            return Err(io_error("bind(endpoint)"));
        }
        // Bind is the first namespace mutation.  Retain the exact inode before
        // any later fallible setup so every subsequent failure can roll back
        // the object we created rather than whatever may occupy its name.
        let mut bound_inode = None;
        let prepared = (|| {
            let object = openat(
                parent.as_raw_fd(),
                spec.name,
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                0,
            )?;
            bound_inode = Some(identity(object.as_raw_fd())?.inode_key());
            let name = component(spec.name)?;
            if unsafe { libc::fchmodat(parent.as_raw_fd(), name.as_ptr(), spec.mode, 0) } < 0 {
                return Err(io_error("fchmodat(endpoint)"));
            }
            if let Some(backlog) = spec.listen_backlog {
                if unsafe { libc::listen(socket.as_raw_fd(), backlog) } < 0 {
                    return Err(io_error("listen(endpoint)"));
                }
            }
            let endpoint_identity = identity(object.as_raw_fd())?;
            validate_owned(endpoint_identity, libc::S_IFSOCK, Some(spec.mode))?;
            if named_identity(parent.as_raw_fd(), spec.name)? != Some(endpoint_identity) {
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
                let named = named_identity(parent.as_raw_fd(), spec.name).ok().flatten();
                let owned = match (bound_inode, named) {
                    (Some(expected), Some(actual)) => actual.inode_key() == expected,
                    (None, Some(_)) => true,
                    _ => false,
                };
                if owned {
                    if let Ok(name) = component(spec.name) {
                        unsafe { libc::unlinkat(parent.as_raw_fd(), name.as_ptr(), 0) };
                    }
                }
                if let Some(directory) = dynamic_directory.as_ref() {
                    if identity(directory.parent.as_raw_fd()).ok()
                        == Some(directory.parent_identity)
                        && identity(parent.as_raw_fd()).ok() == Some(directory.identity)
                        && named_identity(directory.parent.as_raw_fd(), &directory.name)
                            .ok()
                            .flatten()
                            == Some(directory.identity)
                    {
                        if let Ok(name) = component(&directory.name) {
                            unsafe {
                                libc::unlinkat(
                                    directory.parent.as_raw_fd(),
                                    name.as_ptr(),
                                    libc::AT_REMOVEDIR,
                                )
                            };
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
            if identity(endpoint.parent.as_raw_fd())? != endpoint.parent_identity
                || identity(endpoint.object.as_raw_fd())? != endpoint.identity
                || named_identity(endpoint.parent.as_raw_fd(), &endpoint.name)?
                    != Some(endpoint.identity)
            {
                return Err(CohortError::Identity("endpoint replacement"));
            }
            let name = component(&endpoint.name)?;
            if unsafe { libc::unlinkat(endpoint.parent.as_raw_fd(), name.as_ptr(), 0) } < 0 {
                return Err(io_error("unlinkat(endpoint)"));
            }
            endpoint.endpoint_linked = false;
        }
        if let Some(directory) = endpoint.dynamic_directory.as_ref() {
            if identity(directory.parent.as_raw_fd())? != directory.parent_identity
                || identity(endpoint.parent.as_raw_fd())? != directory.identity
                || named_identity(directory.parent.as_raw_fd(), &directory.name)?
                    != Some(directory.identity)
            {
                return Err(CohortError::Identity("dynamic directory replacement"));
            }
            let name = component(&directory.name)?;
            if unsafe {
                libc::unlinkat(
                    directory.parent.as_raw_fd(),
                    name.as_ptr(),
                    libc::AT_REMOVEDIR,
                )
            } < 0
            {
                return Err(io_error("unlinkat(per-user launchd directory)"));
            }
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
                    || named_identity(self.prefix.as_raw_fd(), INIT_PID_NAME)?
                        != Some(retained.identity)
                {
                    return Err(CohortError::Identity("init pid replacement"));
                }
                let name = component(INIT_PID_NAME)?;
                if unsafe { libc::unlinkat(self.prefix.as_raw_fd(), name.as_ptr(), 0) } < 0 {
                    return Err(io_error("unlinkat(init pid)"));
                }
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
    device: u64,
    inode: u64,
    path_len: u16,
    reserved: u16,
    path: [u8; DYNAMIC_PATH_CAPACITY],
}

fn wire_response(
    status: i16,
    endpoint: u16,
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
        device: passed.map_or(0, |identity| identity.device),
        inode: passed.map_or(0, |identity| identity.inode),
        path_len: guest_path.len() as u16,
        reserved: 0,
        path,
    })
}

fn receive_request(fd: RawFd) -> Result<WireRequest, CohortError> {
    let mut request = MaybeUninit::<WireRequest>::zeroed();
    // SAFETY: request storage is writable for exactly one request.
    let count = unsafe { libc::recv(fd, request.as_mut_ptr().cast(), size_of::<WireRequest>(), 0) };
    if count != size_of::<WireRequest>() as isize {
        return Err(CohortError::Protocol("request size"));
    }
    // SAFETY: recv initialized the complete object and every field is integer/byte data.
    Ok(unsafe { request.assume_init() })
}

fn send_response(
    fd: RawFd,
    response: WireResponse,
    passed_fd: Option<RawFd>,
) -> Result<(), CohortError> {
    let mut iov = libc::iovec {
        iov_base: (&response as *const WireResponse).cast_mut().cast(),
        iov_len: size_of::<WireResponse>(),
    };
    let mut message = MaybeUninit::<libc::msghdr>::zeroed();
    let message_ptr = message.as_mut_ptr();
    let mut control = [0u8; 64];
    unsafe {
        (*message_ptr).msg_iov = &mut iov;
        (*message_ptr).msg_iovlen = 1;
        if let Some(passed_fd) = passed_fd {
            (*message_ptr).msg_control = control.as_mut_ptr().cast();
            (*message_ptr).msg_controllen = control.len();
            let header = libc::CMSG_FIRSTHDR(message_ptr);
            if header.is_null() {
                return Err(CohortError::Protocol("SCM_RIGHTS header"));
            }
            (*header).cmsg_level = libc::SOL_SOCKET;
            (*header).cmsg_type = libc::SCM_RIGHTS;
            (*header).cmsg_len = libc::CMSG_LEN(size_of::<RawFd>() as u32) as usize;
            ptr::write(libc::CMSG_DATA(header).cast::<RawFd>(), passed_fd);
            (*message_ptr).msg_controllen = (*header).cmsg_len;
        }
        if libc::sendmsg(fd, message_ptr, libc::MSG_NOSIGNAL | libc::MSG_DONTWAIT)
            != size_of::<WireResponse>() as isize
        {
            return Err(io_error("sendmsg(response)"));
        }
    }
    Ok(())
}

fn peer_credentials(fd: RawFd) -> Result<libc::ucred, CohortError> {
    let mut credentials = MaybeUninit::<libc::ucred>::zeroed();
    let mut length = size_of::<libc::ucred>() as libc::socklen_t;
    if unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            credentials.as_mut_ptr().cast(),
            &mut length,
        )
    } < 0
    {
        return Err(io_error("getsockopt(SO_PEERCRED)"));
    }
    if length as usize != size_of::<libc::ucred>() {
        return Err(CohortError::Protocol("peer credentials"));
    }
    Ok(unsafe { credentials.assume_init() })
}

fn wait_for_io(fd: RawFd, events: i16) -> Result<(), CohortError> {
    let deadline = Instant::now() + Duration::from_millis(REQUEST_TIMEOUT_MS as u64);
    loop {
        let now = Instant::now();
        if now >= deadline {
            return Err(CohortError::Protocol("client request timeout"));
        }
        let remaining = deadline.saturating_duration_since(now);
        let timeout = remaining.as_millis().clamp(1, c_int::MAX as u128) as c_int;
        let mut descriptor = libc::pollfd {
            fd,
            events,
            revents: 0,
        };
        let result = unsafe { libc::poll(&mut descriptor, 1, timeout) };
        if result < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return Err(io_error("poll(client)"));
        }
        if result == 0 {
            return Err(CohortError::Protocol("client request timeout"));
        }
        if descriptor.revents & events != 0 {
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
                    wire_response(-1, request.endpoint, None, &[])?,
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
        let pending = wire_response(0, request.endpoint, Some(socket_identity), &guest_path)?;
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
                let committed = wire_response(0, request.endpoint, None, &[])?;
                // The acknowledgement is diagnostic only.  Once COMMIT was
                // received, delivery failure cannot revoke ownership or
                // unlink the endpoint behind the live consumer's retained FD.
                send_response(client, committed, None)?;
                return Ok(());
            }
            Ok(OPERATION_ABORT) => {
                authority.retire_key(key)?;
                send_response(client, wire_response(0, request.endpoint, None, &[])?, None)?;
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
    let response = wire_response(status, request.endpoint, None, &[])?;
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
        let mut pollfds = [
            libc::pollfd {
                fd: if preserve_acknowledged {
                    -1
                } else {
                    listener.as_raw_fd()
                },
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: control.as_raw_fd(),
                events: libc::POLLIN | libc::POLLHUP | libc::POLLERR,
                revents: 0,
            },
            libc::pollfd {
                fd: parent_watch.as_ref().map_or(-1, AsRawFd::as_raw_fd),
                events: libc::POLLIN,
                revents: 0,
            },
        ];
        let polled =
            unsafe { libc::poll(pollfds.as_mut_ptr(), pollfds.len() as _, REQUEST_TIMEOUT_MS) };
        if polled < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            forensic_preserve = true;
            break Err(io_error("poll(controller)"));
        }
        if pollfds[1].revents & (libc::POLLIN | libc::POLLHUP | libc::POLLERR) != 0 {
            let mut command = [0u8; 2];
            let received = unsafe {
                libc::recv(
                    control.as_raw_fd(),
                    command.as_mut_ptr().cast(),
                    command.len(),
                    0,
                )
            };
            let exact = received == 1;
            if exact && command[0] == CONTROLLER_COMMAND_CLEANUP {
                // Receipt proves the parent's successful send commit. Cleanup
                // remains committed even if the diagnostic ACK is lost.
                let acknowledged = [CONTROLLER_COMMAND_ACK];
                let _ = write_all(control.as_raw_fd(), &acknowledged);
                forensic_preserve = false;
                break Ok(());
            }
            if !exact || command[0] != CONTROLLER_COMMAND_PRESERVE {
                forensic_preserve = true;
                break Ok(());
            }
            let acknowledged = [CONTROLLER_COMMAND_ACK];
            if write_all(control.as_raw_fd(), &acknowledged).is_err() {
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
        if pollfds[2].revents & libc::POLLIN != 0 {
            forensic_preserve = true;
            break Ok(());
        }
        if pollfds[0].revents & libc::POLLIN == 0 {
            continue;
        }
        let client = unsafe {
            libc::accept4(
                listener.as_raw_fd(),
                ptr::null_mut(),
                ptr::null_mut(),
                libc::SOCK_CLOEXEC,
            )
        };
        if client < 0 {
            continue;
        }
        let client = unsafe { OwnedFd::from_raw_fd(client) };
        if let Err(error) = serve_client(&mut authority, client.as_raw_fd(), &nonce) {
            if rejected_requests == 0 {
                eprintln!("lifecycle cohort request rejected: {error}");
            }
            if let Ok(response) = wire_response(-1, 0, None, &[]) {
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

struct ProcessWorker {
    pid: libc::pid_t,
    pidfd: OwnedFd,
}

fn pidfd_open(pid: libc::pid_t) -> Result<OwnedFd, CohortError> {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) as c_int };
    if fd < 0 {
        return Err(io_error("pidfd_open(controller)"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
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
    if unsafe { libc::kill(worker.pid, libc::SIGKILL) } < 0
        && io::Error::last_os_error().raw_os_error() != Some(libc::ESRCH)
    {
        return Err(io_error("kill(abandon controller)"));
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
    let mut status = 0;
    let waited = unsafe { libc::waitpid(worker.pid, &mut status, libc::WNOHANG) };
    if waited == worker.pid {
        Ok(())
    } else if waited == 0 {
        Err(CohortError::Protocol("abandon waitpid pending"))
    } else {
        Err(io_error("waitpid(abandon controller)"))
    }
}

#[cfg(not(test))]
fn close_unowned_child_fds(allowed: &[RawFd]) -> Result<(), CohortError> {
    let proc_fds = CString::new("/proc/self/fd").expect("static path");
    let directory = unsafe { libc::opendir(proc_fds.as_ptr()) };
    if directory.is_null() {
        return Err(io_error("opendir(controller fds)"));
    }
    let directory_fd = unsafe { libc::dirfd(directory) };
    let mut close_list = [0; 4096];
    let mut count = 0usize;
    loop {
        unsafe { *libc::__errno_location() = 0 };
        let entry = unsafe { libc::readdir(directory) };
        if entry.is_null() {
            let error = io::Error::last_os_error();
            unsafe { libc::closedir(directory) };
            if error.raw_os_error() != Some(0) {
                return Err(CohortError::Io("readdir(controller fds)", error));
            }
            break;
        }
        let name = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        let Some(fd) = std::str::from_utf8(name.to_bytes())
            .ok()
            .and_then(|value| value.parse::<RawFd>().ok())
        else {
            continue;
        };
        if fd <= 2 || fd == directory_fd || allowed.contains(&fd) {
            continue;
        }
        if count == close_list.len() {
            unsafe { libc::closedir(directory) };
            return Err(CohortError::Protocol("inherited fd budget"));
        }
        close_list[count] = fd;
        count += 1;
    }
    for fd in &close_list[..count] {
        unsafe { libc::close(*fd) };
    }
    Ok(())
}

#[cfg(not(test))]
fn wait_worker(worker: ProcessWorker) -> Result<(), CohortError> {
    let mut descriptor = libc::pollfd {
        fd: worker.pidfd.as_raw_fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    let polled = loop {
        let result = unsafe { libc::poll(&mut descriptor, 1, CONTROLLER_EXIT_TIMEOUT_MS) };
        if result < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        break result;
    };
    let forced = polled <= 0;
    if forced {
        unsafe { libc::kill(worker.pid, libc::SIGKILL) };
    }
    let mut status = 0;
    loop {
        let waited = unsafe { libc::waitpid(worker.pid, &mut status, 0) };
        if waited == worker.pid {
            break;
        }
        if waited < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        return Err(io_error("waitpid(controller)"));
    }
    if !forced && libc::WIFEXITED(status) && libc::WEXITSTATUS(status) == 0 {
        Ok(())
    } else {
        Err(CohortError::Process)
    }
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
    Abandoning,
    Finished,
}

impl CohortController {
    pub fn start(prefix: &Path, init_pid: libc::pid_t) -> Result<(Self, OwnedFd), CohortError> {
        let authority = SessionAuthority::acquire(prefix, init_pid)?;
        Self::start_with_authority(authority, Some(prefix), false)
    }

    fn start_from_fd(
        prefix_fd: RawFd,
        prefix_argument: &[u8],
        init_pid: libc::pid_t,
    ) -> Result<(Self, OwnedFd), CohortError> {
        let authority = SessionAuthority::acquire_from_fd(prefix_fd, prefix_argument, init_pid)?;
        #[cfg(test)]
        let test_prefix = Some(Path::new(std::ffi::OsStr::from_bytes(prefix_argument)));
        #[cfg(not(test))]
        let test_prefix = None;
        Self::start_with_authority(authority, test_prefix, false)
    }

    fn start_with_authority(
        mut authority: SessionAuthority,
        test_prefix: Option<&Path>,
        allow_test_peer: bool,
    ) -> Result<(Self, OwnedFd), CohortError> {
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
        let name = CONTROL_GUEST_PATH.to_vec();
        let listener = authority.publish(CohortEndpoint::Control)?;
        let mut control = [0; 2];
        if unsafe {
            libc::socketpair(
                libc::AF_UNIX,
                libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                0,
                control.as_mut_ptr(),
            )
        } < 0
        {
            return Err(io_error("socketpair(controller command)"));
        }
        let shutdown = unsafe { OwnedFd::from_raw_fd(control[0]) };
        let child_control = unsafe { OwnedFd::from_raw_fd(control[1]) };

        #[cfg(test)]
        let controller = {
            let thread = thread::Builder::new()
                .name("darling-lifecycle-cohort".to_string())
                .spawn(move || server_loop(authority, listener, child_control, None, nonce))
                .map_err(|error| CohortError::Io("spawn(controller)", error))?;
            Self {
                shutdown,
                dserver_log: Some(dserver_log),
                thread: Some(thread),
                control_name: name,
                nonce,
                guest_namespace,
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
            let parent_pid = unsafe { libc::getpid() };
            let parent_watch = pidfd_open(parent_pid)?;
            let mut ready = [0; 2];
            if unsafe {
                libc::socketpair(
                    libc::AF_UNIX,
                    libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                    0,
                    ready.as_mut_ptr(),
                )
            } < 0
            {
                return Err(io_error("socketpair(controller ready)"));
            }
            let ready_parent = unsafe { OwnedFd::from_raw_fd(ready[0]) };
            let ready_child = unsafe { OwnedFd::from_raw_fd(ready[1]) };
            let child = unsafe { libc::fork() };
            if child < 0 {
                return Err(io_error("fork(controller)"));
            }
            if child == 0 {
                drop(ready_parent);
                unsafe { libc::close(shutdown.as_raw_fd()) };
                unsafe { libc::close(darlingserver.as_raw_fd()) };
                if unsafe { libc::setsid() } < 0 {
                    let ready_status: i32 = -1;
                    let _ = write_all(ready_child.as_raw_fd(), &ready_status.to_ne_bytes());
                    authority.disarm_drop_cleanup();
                    unsafe { libc::_exit(1) };
                }
                let mut allowed = authority.retained_fds();
                allowed.extend([
                    listener.as_raw_fd(),
                    child_control.as_raw_fd(),
                    parent_watch.as_raw_fd(),
                    ready_child.as_raw_fd(),
                ]);
                let prepared = close_unowned_child_fds(&allowed);
                let ready_status: i32 = if prepared.is_ok() { 0 } else { -1 };
                let _ = write_all(ready_child.as_raw_fd(), &ready_status.to_ne_bytes());
                drop(ready_child);
                if prepared.is_err() {
                    authority.disarm_drop_cleanup();
                    unsafe { libc::_exit(1) };
                }
                let (mut authority, loop_result, forensic_preserve) = server_loop(
                    authority,
                    listener,
                    child_control,
                    Some(parent_watch),
                    nonce,
                );
                let cleanup_result = if forensic_preserve {
                    authority.disarm_drop_cleanup();
                    Ok(())
                } else {
                    authority.cleanup_all()
                };
                let status = i32::from(loop_result.is_err() || cleanup_result.is_err());
                unsafe { libc::_exit(status) };
            }
            drop(ready_child);
            drop(child_control);
            authority.disarm_drop_cleanup();
            drop(authority);
            drop(listener);
            drop(parent_watch);
            let child_pidfd = match pidfd_open(child) {
                Ok(pidfd) => pidfd,
                Err(error) => {
                    unsafe { libc::kill(child, libc::SIGKILL) };
                    let mut status = 0;
                    unsafe { libc::waitpid(child, &mut status, 0) };
                    return Err(error);
                }
            };
            let process = ProcessWorker {
                pid: child,
                pidfd: child_pidfd,
            };
            if let Err(error) = wait_for_io(ready_parent.as_raw_fd(), libc::POLLIN) {
                unsafe { libc::kill(child, libc::SIGKILL) };
                let _ = wait_worker(process);
                return Err(error);
            }
            let mut ready_status = -1i32;
            if unsafe {
                libc::recv(
                    ready_parent.as_raw_fd(),
                    (&mut ready_status as *mut i32).cast(),
                    size_of::<i32>(),
                    0,
                )
            } != size_of::<i32>() as isize
                || ready_status != 0
            {
                unsafe { libc::kill(child, libc::SIGKILL) };
                let _ = wait_worker(process);
                return Err(CohortError::Process);
            }
            Self {
                shutdown,
                dserver_log: Some(dserver_log),
                process: Some(process),
                control_name: name,
                nonce,
                guest_namespace,
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
        init_pid: libc::pid_t,
    ) -> Result<(Self, OwnedFd), CohortError> {
        let authority = SessionAuthority::acquire(prefix, init_pid)?;
        Self::start_with_authority(authority, Some(prefix), true)
    }

    pub fn control_name(&self) -> &[u8] {
        &self.control_name
    }

    pub fn nonce_hex(&self) -> [u8; NONCE_HEX_BYTES] {
        nonce_hex(&self.nonce)
    }

    pub fn send_guest_namespace_bootstrap(&self, socket: RawFd) -> Result<(), CohortError> {
        self.guest_namespace
            .as_ref()
            .ok_or(CohortError::Protocol(
                "guest namespace authority unavailable",
            ))?
            .send_bootstrap(socket)
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
        let sent = unsafe {
            libc::send(
                self.shutdown.as_raw_fd(),
                (&command as *const u8).cast(),
                1,
                libc::MSG_NOSIGNAL,
            )
        };
        if sent != 1 {
            return Err(io_error("send(controller command)"));
        }
        Ok(())
    }

    fn receive_controller_ack(&self) -> Result<(), CohortError> {
        wait_for_io(self.shutdown.as_raw_fd(), libc::POLLIN)?;
        let mut acknowledged = [0u8; 2];
        let received = unsafe {
            libc::recv(
                self.shutdown.as_raw_fd(),
                acknowledged.as_mut_ptr().cast(),
                acknowledged.len(),
                0,
            )
        };
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
            self.cleanup_phase = CleanupPhase::Finished;
            Ok(())
        }
        #[cfg(not(test))]
        {
            wait_worker(self.process.take().ok_or(CohortError::Process)?)?;
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

#[must_use = "a drain-pending controller cannot be discarded as cleanup success"]
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
            Self::Terminal(error) => formatter.debug_tuple("Terminal").field(error).finish(),
        }
    }
}

#[no_mangle]
/// Start one retained first-cohort controller.
///
/// # Safety
///
/// `prefix_fd` must be a live directory descriptor, `prefix_argument` must
/// point to the live NUL-terminated prefix argument passed to launchd, and
/// `output` must point to writable `CohortBootstrap` storage. The
/// returned pointer must be consumed exactly once by
/// [`darling_lifecycle_cohort_finish`].
pub unsafe extern "C" fn darling_lifecycle_cohort_start(
    prefix_fd: c_int,
    prefix_argument: *const c_char,
    init_pid: libc::pid_t,
    output: *mut CohortBootstrap,
) -> *mut CohortController {
    if prefix_fd < 0 || prefix_argument.is_null() || output.is_null() {
        return ptr::null_mut();
    }
    let prefix_argument = CStr::from_ptr(prefix_argument);
    let (mut controller, darlingserver) =
        match CohortController::start_from_fd(prefix_fd, prefix_argument.to_bytes(), init_pid) {
            Ok(value) => value,
            Err(error) => {
                eprintln!("lifecycle cohort acquisition refused: {error}");
                return ptr::null_mut();
            }
        };
    let dserver_log = match controller.take_dserver_log() {
        Ok(log) => log,
        Err(error) => {
            eprintln!("lifecycle cohort log transfer refused: {error}");
            return ptr::null_mut();
        }
    };
    if controller.control_name.len() > CONTROL_NAME_CAPACITY {
        return ptr::null_mut();
    }
    let mut bootstrap = CohortBootstrap {
        darlingserver_fd: darlingserver.into_raw_fd(),
        dserver_log_fd: dserver_log.into_raw_fd(),
        control_name_len: controller.control_name.len() as u16,
        reserved: 0,
        control_name: [0; CONTROL_NAME_CAPACITY],
        nonce_hex: controller.nonce_hex(),
    };
    bootstrap.control_name[..controller.control_name.len()]
        .copy_from_slice(&controller.control_name);
    ptr::write(output, bootstrap);
    Box::into_raw(Box::new(controller))
}

#[no_mangle]
/// Consume a controller and perform bounded cohort cleanup.
///
/// # Safety
///
/// `controller` must be a non-null pointer returned by
/// [`darling_lifecycle_cohort_start`] that has not already been consumed.
pub unsafe extern "C" fn darling_lifecycle_cohort_finish(
    controller: *mut CohortController,
) -> c_int {
    if controller.is_null() {
        return -1;
    }
    let mut owned = Box::from_raw(controller);
    if owned.cleanup_phase == CleanupPhase::CleanupCommitted {
        return if (*owned).finish_after_cleanup_commit().is_ok() {
            0
        } else {
            -1
        };
    }
    if owned.cleanup_phase == CleanupPhase::Abandoning {
        let restored = Box::into_raw(owned);
        debug_assert_eq!(restored, controller);
        return 1;
    }
    owned.cleanup_phase = CleanupPhase::Draining;
    let Some(guest_namespace) = owned.guest_namespace.as_mut() else {
        return -1;
    };
    if guest_namespace.revoke().is_err() {
        let restored = Box::into_raw(owned);
        debug_assert_eq!(restored, controller);
        return 1;
    }
    if owned.request_cleanup().is_err() {
        let status = if owned.cleanup_phase == CleanupPhase::CleanupCommitted {
            CLEANUP_PENDING_STATUS
        } else {
            1
        };
        let restored = Box::into_raw(owned);
        debug_assert_eq!(restored, controller);
        return status;
    }
    if (*owned).finish_after_cleanup_commit().is_ok() {
        0
    } else {
        -1
    }
}

#[no_mangle]
/// Consume a revoked controller without destructive namespace cleanup.
///
/// # Safety
/// `controller` must be the live pointer retained after
/// `DARLING_LIFECYCLE_FINISH_DRAIN_PENDING`. A zero result consumes it;
/// `DARLING_LIFECYCLE_ABANDON_PENDING` preserves the exact pointer for retry.
pub unsafe extern "C" fn darling_lifecycle_cohort_abandon(
    controller: *mut CohortController,
) -> c_int {
    if controller.is_null() {
        return -1;
    }
    let mut owned = Box::from_raw(controller);
    if matches!(
        owned.cleanup_phase,
        CleanupPhase::CleanupRequested | CleanupPhase::CleanupCommitted | CleanupPhase::Finished
    ) {
        let restored = Box::into_raw(owned);
        debug_assert_eq!(restored, controller);
        return ABANDON_PENDING_STATUS;
    }
    let Some(guest_namespace) = owned.guest_namespace.as_mut() else {
        return -1;
    };
    // revoke() publishes REVOKED before a possible DrainPending result.
    let _ = guest_namespace.revoke();
    // The transition may fail before exit/reap is proven. Preserve the exact
    // allocation and ProcessWorker for a later retry in that case.
    match owned.abandon_after_revocation() {
        Ok(()) => 0,
        Err(_) => {
            let restored = Box::into_raw(owned);
            debug_assert_eq!(restored, controller);
            ABANDON_PENDING_STATUS
        }
    }
}

#[no_mangle]
/// Send the exact authenticated guest namespace capability set over a trusted
/// `SOCK_SEQPACKET` bootstrap socket before mldr enters guest code.
///
/// # Safety
///
/// `controller` must be the live pointer returned by
/// [`darling_lifecycle_cohort_start`], and `socket_fd` must be owned by the
/// caller for the duration of this call.
pub unsafe extern "C" fn darling_lifecycle_cohort_send_guest_namespace_bootstrap(
    controller: *mut CohortController,
    socket_fd: c_int,
) -> c_int {
    let Some(controller) = controller.as_ref() else {
        return -1;
    };
    if socket_fd < 0
        || controller
            .send_guest_namespace_bootstrap(socket_fd)
            .is_err()
    {
        -1
    } else {
        0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{symlink, FileTypeExt, MetadataExt, PermissionsExt};
    use std::path::PathBuf;
    use std::process::Command;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT_TEST: AtomicU64 = AtomicU64::new(1);

    struct Fixture {
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "darling-lifecycle-cohort-{}-{}",
                std::process::id(),
                NEXT_TEST.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            fs::create_dir_all(root.join("var/run")).unwrap();
            fs::create_dir_all(root.join("var/tmp/launchd")).unwrap();
            fs::set_permissions(root.join("var"), fs::Permissions::from_mode(0o755)).unwrap();
            fs::set_permissions(root.join("var/run"), fs::Permissions::from_mode(0o755)).unwrap();
            fs::set_permissions(root.join("var/tmp"), fs::Permissions::from_mode(0o1777)).unwrap();
            fs::set_permissions(
                root.join("var/tmp/launchd"),
                fs::Permissions::from_mode(0o700),
            )
            .unwrap();
            let metadata = fs::metadata(&root).unwrap();
            let state = format!(
                "DARLING_PREFIX_STATE_V2\n\
                 schema_version=2\n\
                 runtime_mode=rootless-eunion\n\
                 generation=1\n\
                 prefix_device={}\n\
                 prefix_inode={}\n\
                 owner_uid={}\n\
                 owner_gid={}\n\
                 provenance=darling-runtime-prefix-lifecycle-v2\n",
                metadata.dev(),
                metadata.ino(),
                metadata.uid(),
                metadata.gid(),
            );
            fs::write(root.join(".darling-prefix-state-v2"), state).unwrap();
            fs::set_permissions(
                root.join(".darling-prefix-state-v2"),
                fs::Permissions::from_mode(0o600),
            )
            .unwrap();
            Self { root }
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    fn connect(path: &Path) -> OwnedFd {
        let fd =
            unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0) };
        assert!(fd >= 0);
        let fd = unsafe { OwnedFd::from_raw_fd(fd) };
        let mut address = MaybeUninit::<libc::sockaddr_un>::zeroed();
        let address_ptr = address.as_mut_ptr();
        let name = path.as_os_str().as_bytes();
        assert!(name.len() < unsafe { (*address_ptr).sun_path.len() });
        unsafe {
            (*address_ptr).sun_family = libc::AF_UNIX as libc::sa_family_t;
            ptr::copy_nonoverlapping(
                name.as_ptr().cast::<c_char>(),
                (*address_ptr).sun_path.as_mut_ptr(),
                name.len(),
            );
        }
        let length = (size_of::<libc::sa_family_t>() + name.len() + 1) as libc::socklen_t;
        assert_eq!(
            unsafe { libc::connect(fd.as_raw_fd(), address_ptr.cast(), length) },
            0
        );
        fd
    }

    #[test]
    fn dserver_log_is_opened_fd_relative_and_persists_after_clean_finish() {
        let fixture = Fixture::new();
        let (mut controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let log = controller.take_dserver_log().unwrap();
        write_all(log.as_raw_fd(), b"cohort-main-log\n").unwrap();
        let descriptor_identity = identity(log.as_raw_fd()).unwrap();
        let path = fixture.root.join("private/var/log/dserver.log");
        let metadata = fs::metadata(&path).unwrap();
        assert_eq!(metadata.dev(), descriptor_identity.device);
        assert_eq!(metadata.ino(), descriptor_identity.inode);
        assert_eq!(metadata.mode() & 0o777, 0o644);
        controller.finish().unwrap();
        assert_eq!(fs::read(&path).unwrap(), b"cohort-main-log\n");
        assert!(!fixture
            .root
            .join("private/var/log/dserver-auxlog.txt")
            .exists());
    }

    #[test]
    fn dserver_log_replacement_is_preserved_and_finish_fails_closed() {
        let fixture = Fixture::new();
        let (mut controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let log = controller.take_dserver_log().unwrap();
        let original = identity(log.as_raw_fd()).unwrap();
        let directory = fixture.root.join("private/var/log");
        let path = directory.join("dserver.log");
        let retained = directory.join("dserver.log.retained");
        fs::rename(&path, &retained).unwrap();
        fs::write(&path, b"replacement\n").unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(controller.finish().is_err());
        assert_eq!(fs::read(&path).unwrap(), b"replacement\n");
        let metadata = fs::metadata(&retained).unwrap();
        assert_eq!(
            (metadata.dev(), metadata.ino()),
            (original.device, original.inode)
        );
    }

    #[test]
    fn guest_mutation_drain_pending_preserves_owning_controller_for_retry() {
        let fixture = Fixture::new();
        let (controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let mut sockets = [0; 2];
        assert_eq!(
            unsafe {
                libc::socketpair(
                    libc::AF_UNIX,
                    libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                    0,
                    sockets.as_mut_ptr(),
                )
            },
            0
        );
        controller
            .send_guest_namespace_bootstrap(sockets[0])
            .unwrap();
        let session =
            crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
                .unwrap();
        unsafe {
            libc::close(sockets[0]);
            libc::close(sockets[1]);
        }
        let mutation = session.authorize().unwrap();
        let control_path = controller.control_test_path.clone();
        let pending = match controller.finish() {
            Err(CohortFinishError::DrainPending { controller, .. }) => controller,
            other => panic!("expected owning drain pending, got {other:?}"),
        };
        assert!(control_path.exists());
        assert!(matches!(
            session.authorize(),
            Err(crate::guest_namespace_authority::AuthorityError::Revoked)
        ));
        drop(mutation);
        pending.finish().unwrap();
        assert!(!control_path.exists());
    }

    #[test]
    fn c_abi_drain_pending_preserves_exact_pointer_for_retry() {
        let fixture = Fixture::new();
        let (controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let mut sockets = [0; 2];
        assert_eq!(
            unsafe {
                libc::socketpair(
                    libc::AF_UNIX,
                    libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                    0,
                    sockets.as_mut_ptr(),
                )
            },
            0
        );
        controller
            .send_guest_namespace_bootstrap(sockets[0])
            .unwrap();
        let session =
            crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
                .unwrap();
        unsafe {
            libc::close(sockets[0]);
            libc::close(sockets[1]);
        }
        let mutation = session.authorize().unwrap();
        let pointer = Box::into_raw(Box::new(controller));
        assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 1);
        assert!(matches!(
            session.authorize(),
            Err(crate::guest_namespace_authority::AuthorityError::Revoked)
        ));
        drop(mutation);
        assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 0);
    }

    fn assert_unknown_controller_command_preserves(command: u8) {
        let fixture = Fixture::new();
        let (mut controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let path = controller.control_test_path.clone();
        let before = fs::symlink_metadata(&path).unwrap();
        assert_eq!(
            unsafe {
                libc::send(
                    controller.shutdown.as_raw_fd(),
                    (&command as *const u8).cast(),
                    1,
                    libc::MSG_NOSIGNAL,
                )
            },
            1
        );
        let (authority, result, forensic_preserve) =
            controller.thread.take().unwrap().join().unwrap();
        assert!(result.is_ok());
        assert!(forensic_preserve);
        drop(authority);
        let after = fs::symlink_metadata(&path).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
    }

    #[test]
    fn accumulated_eventfd_values_four_and_six_are_unknown_and_preserve() {
        assert_unknown_controller_command_preserves(4);
        assert_unknown_controller_command_preserves(6);
    }

    #[test]
    fn missing_preserve_ack_never_reenables_cleanup() {
        let fixture = Fixture::new();
        let (mut controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let path = controller.control_test_path.clone();
        let before = fs::symlink_metadata(&path).unwrap();
        controller.forensic_preserve_requested = true;
        assert_eq!(
            unsafe { libc::shutdown(controller.shutdown.as_raw_fd(), libc::SHUT_RDWR) },
            0
        );
        assert!(controller.request_forensic_preserve().is_err());
        assert!(controller.request_cleanup().is_err());
        let (authority, _, forensic_preserve) = controller.thread.take().unwrap().join().unwrap();
        assert!(forensic_preserve);
        drop(authority);
        let after = fs::symlink_metadata(&path).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
    }

    #[test]
    fn post_send_cleanup_ack_loss_only_reaps_and_never_abandons() {
        let fixture = Fixture::new();
        let (mut controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let path = controller.control_test_path.clone();
        controller.lose_cleanup_ack_after_receive = true;
        let pointer = Box::into_raw(Box::new(controller));
        assert_eq!(
            unsafe { darling_lifecycle_cohort_finish(pointer) },
            CLEANUP_PENDING_STATUS
        );
        assert_eq!(
            unsafe { (*pointer).cleanup_phase },
            CleanupPhase::CleanupCommitted
        );
        // CLEANUP is committed. A raw abandon attempt is refused while
        // preserving the exact owning pointer and cannot select forensics.
        assert_eq!(
            unsafe { darling_lifecycle_cohort_abandon(pointer) },
            ABANDON_PENDING_STATUS
        );
        assert_eq!(
            unsafe { (*pointer).cleanup_phase },
            CleanupPhase::CleanupCommitted
        );
        assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 0);
        assert!(!path.exists());
    }

    #[test]
    fn c_abi_abandon_stops_worker_without_destructive_cleanup() {
        let fixture = Fixture::new();
        let (controller, listener) =
            CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
        drop(listener);
        let control_path = controller.control_test_path.clone();
        let mut sockets = [0; 2];
        assert_eq!(
            unsafe {
                libc::socketpair(
                    libc::AF_UNIX,
                    libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                    0,
                    sockets.as_mut_ptr(),
                )
            },
            0
        );
        controller
            .send_guest_namespace_bootstrap(sockets[0])
            .unwrap();
        let session =
            crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
                .unwrap();
        unsafe {
            libc::close(sockets[0]);
            libc::close(sockets[1]);
        }
        let mutation = session.authorize().unwrap();
        let mut controller = controller;
        controller.abandon_fault = Some(AbandonFault::WaitpidError);
        let pointer = Box::into_raw(Box::new(controller));
        assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 1);
        assert_eq!(
            unsafe { darling_lifecycle_cohort_abandon(pointer) },
            ABANDON_PENDING_STATUS
        );
        assert!(control_path.exists());
        assert!(matches!(
            session.authorize(),
            Err(crate::guest_namespace_authority::AuthorityError::Revoked)
        ));
        assert_eq!(unsafe { darling_lifecycle_cohort_abandon(pointer) }, 0);
        assert!(control_path.exists());
        assert!(matches!(
            session.authorize(),
            Err(crate::guest_namespace_authority::AuthorityError::Revoked)
        ));
        drop(mutation);
        drop(session);
    }

    #[test]
    fn real_process_abandon_faults_preserve_worker_until_retry_reaps() {
        for fault in [
            AbandonFault::KillError,
            AbandonFault::PidfdTimeout,
            AbandonFault::WaitpidEintr,
            AbandonFault::WaitpidZero,
            AbandonFault::WaitpidError,
        ] {
            let child = unsafe { libc::fork() };
            assert!(child >= 0);
            if child == 0 {
                loop {
                    unsafe { libc::pause() };
                }
            }
            let worker = ProcessWorker {
                pid: child,
                pidfd: pidfd_open(child).unwrap(),
            };
            assert!(abandon_process_worker(&worker, Some(fault)).is_err());
            assert!(unsafe { libc::fcntl(worker.pidfd.as_raw_fd(), libc::F_GETFD) } >= 0);
            if fault == AbandonFault::KillError {
                assert_eq!(unsafe { libc::kill(child, 0) }, 0);
            }
            abandon_process_worker(&worker, None).unwrap();
            let mut status = 0;
            assert_eq!(
                unsafe { libc::waitpid(child, &mut status, libc::WNOHANG) },
                -1
            );
            assert_eq!(
                io::Error::last_os_error().raw_os_error(),
                Some(libc::ECHILD)
            );
        }
    }

    #[test]
    fn dserver_log_symlink_is_rejected_before_writer_transfer() {
        let fixture = Fixture::new();
        let directory = fixture.root.join("private/var/log");
        fs::create_dir_all(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
        fs::write(directory.join("outside"), b"preserved\n").unwrap();
        symlink("outside", directory.join("dserver.log")).unwrap();
        assert!(CohortController::start_for_test(&fixture.root, std::process::id() as _).is_err());
        assert_eq!(fs::read(directory.join("outside")).unwrap(), b"preserved\n");
    }

    #[test]
    fn dserver_log_fifo_is_rejected_without_opening_a_writer() {
        let fixture = Fixture::new();
        let directory = fixture.root.join("private/var/log");
        fs::create_dir_all(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
        let fifo = CString::new(directory.join("dserver.log").as_os_str().as_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o644) }, 0);
        let started = Instant::now();
        assert!(CohortController::start_for_test(&fixture.root, std::process::id() as _).is_err());
        assert!(started.elapsed() < Duration::from_secs(1));
        assert!(fs::symlink_metadata(directory.join("dserver.log"))
            .unwrap()
            .file_type()
            .is_fifo());
    }

    #[test]
    fn dserver_log_creation_normalizes_aggressive_umask_inode_bound() {
        const CHILD: &str = "DARLING_LIFECYCLE_UMASK_TEST_CHILD";
        if std::env::var_os(CHILD).is_none() {
            let status = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "cohort_routing::tests::dserver_log_creation_normalizes_aggressive_umask_inode_bound",
                    "--nocapture",
                ])
                .env(CHILD, "1")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        let fixture = Fixture::new();
        let previous = unsafe { libc::umask(0o077) };
        let result = CohortController::start_for_test(&fixture.root, std::process::id() as _);
        unsafe { libc::umask(previous) };
        let (mut controller, listener) = result.unwrap();
        drop(listener);
        let log = controller.take_dserver_log().unwrap();
        assert_eq!(identity(log.as_raw_fd()).unwrap().mode & 0o777, 0o644);
        controller.finish().unwrap();
        assert_eq!(
            fs::metadata(fixture.root.join("private/var/log/dserver.log"))
                .unwrap()
                .mode()
                & 0o777,
            0o644
        );
    }

    #[test]
    fn dserver_log_created_inode_rollback_is_exact_and_replacement_safe() {
        let fixture = Fixture::new();
        let directory = fixture.root.join("private/var/log");
        fs::create_dir_all(&directory).unwrap();
        fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
        let parent = open_directory_chain(
            open_prefix(&fixture.root).unwrap().as_raw_fd(),
            DSERVER_LOG_PARENT,
        )
        .unwrap();
        let created = openat(
            parent.as_raw_fd(),
            DSERVER_LOG_NAME,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            0o600,
        )
        .unwrap();
        let created_identity = identity(created.as_raw_fd()).unwrap();
        rollback_created_log(parent.as_raw_fd(), DSERVER_LOG_NAME, created_identity).unwrap();
        assert!(!directory.join("dserver.log").exists());

        let original = openat(
            parent.as_raw_fd(),
            DSERVER_LOG_NAME,
            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            0o600,
        )
        .unwrap();
        let original_identity = identity(original.as_raw_fd()).unwrap();
        fs::rename(directory.join("dserver.log"), directory.join("retained")).unwrap();
        fs::write(directory.join("dserver.log"), b"replacement\n").unwrap();
        assert!(
            rollback_created_log(parent.as_raw_fd(), DSERVER_LOG_NAME, original_identity).is_err()
        );
        assert_eq!(
            fs::read(directory.join("dserver.log")).unwrap(),
            b"replacement\n"
        );
        assert_eq!(
            fs::metadata(directory.join("retained")).unwrap().ino(),
            original_identity.inode
        );
    }

    #[test]
    fn dserver_log_post_create_failure_removes_exact_inode() {
        let fixture = Fixture::new();
        let mut authority =
            SessionAuthority::acquire(&fixture.root, std::process::id() as _).unwrap();
        authority.fail_log_after_create = true;
        assert!(matches!(
            authority.publish_log(),
            Err(CohortError::Protocol("injected post-create log failure"))
        ));
        assert!(!fixture.root.join("private/var/log/dserver.log").exists());
        assert!(authority.log.is_none());
    }

    #[test]
    fn dserver_log_bootstrap_transfers_exact_writer_fd_once() {
        let fixture = Fixture::new();
        let prefix = open_prefix(&fixture.root).unwrap();
        let prefix_argument = CString::new(fixture.root.as_os_str().as_bytes()).unwrap();
        let mut bootstrap = MaybeUninit::<CohortBootstrap>::zeroed();
        let controller = unsafe {
            darling_lifecycle_cohort_start(
                prefix.as_raw_fd(),
                prefix_argument.as_ptr(),
                std::process::id() as _,
                bootstrap.as_mut_ptr(),
            )
        };
        assert!(!controller.is_null());
        let bootstrap = unsafe { bootstrap.assume_init() };
        assert!(bootstrap.darlingserver_fd >= 0);
        assert!(bootstrap.dserver_log_fd >= 0);
        write_all(bootstrap.dserver_log_fd, b"ffi-bootstrap-log\n").unwrap();
        let expected = identity(bootstrap.dserver_log_fd).unwrap();
        assert_eq!(unsafe { darling_lifecycle_cohort_finish(controller) }, 0);
        let path = fixture.root.join("private/var/log/dserver.log");
        let metadata = fs::metadata(&path).unwrap();
        assert_eq!(
            (metadata.dev(), metadata.ino()),
            (expected.device, expected.inode)
        );
        assert_eq!(fs::read(&path).unwrap(), b"ffi-bootstrap-log\n");
        unsafe {
            libc::close(bootstrap.darlingserver_fd);
            libc::close(bootstrap.dserver_log_fd);
        }
    }

    fn request(
        controller: &CohortController,
        kind: CohortEndpoint,
        operation: u16,
        nonce: [u8; 32],
    ) -> WireResponse {
        let client = connect(&controller.control_test_path);
        let request = WireRequest {
            magic: PROTOCOL_MAGIC,
            version: PROTOCOL_VERSION,
            operation,
            endpoint: kind as u16,
            reserved: 0,
            nonce,
        };
        assert_eq!(
            unsafe {
                libc::send(
                    client.as_raw_fd(),
                    (&request as *const WireRequest).cast(),
                    size_of::<WireRequest>(),
                    libc::MSG_NOSIGNAL,
                )
            },
            size_of::<WireRequest>() as isize
        );
        let mut response = MaybeUninit::<WireResponse>::zeroed();
        assert_eq!(
            unsafe {
                libc::recv(
                    client.as_raw_fd(),
                    response.as_mut_ptr().cast(),
                    size_of::<WireResponse>(),
                    0,
                )
            },
            size_of::<WireResponse>() as isize
        );
        let response = unsafe { response.assume_init() };
        if operation == OPERATION_PUBLISH && response.status == 0 {
            let decision = WireRequest {
                magic: PROTOCOL_MAGIC,
                version: PROTOCOL_VERSION,
                operation: OPERATION_COMMIT,
                endpoint: kind as u16,
                reserved: 0,
                nonce,
            };
            assert_eq!(
                unsafe {
                    libc::send(
                        client.as_raw_fd(),
                        (&decision as *const WireRequest).cast(),
                        size_of::<WireRequest>(),
                        libc::MSG_NOSIGNAL,
                    )
                },
                size_of::<WireRequest>() as isize
            );
            let committed = receive_response_only(client.as_raw_fd());
            assert_eq!(committed.status, 0);
            assert_eq!(committed.endpoint, kind as u16);
            assert_eq!(committed.has_fd, 0);
        }
        response
    }

    fn send_request_only(
        client: RawFd,
        kind: CohortEndpoint,
        operation: u16,
        nonce: [u8; NONCE_BYTES],
    ) {
        let request = WireRequest {
            magic: PROTOCOL_MAGIC,
            version: PROTOCOL_VERSION,
            operation,
            endpoint: kind as u16,
            reserved: 0,
            nonce,
        };
        assert_eq!(
            unsafe {
                libc::send(
                    client,
                    (&request as *const WireRequest).cast(),
                    size_of::<WireRequest>(),
                    libc::MSG_NOSIGNAL,
                )
            },
            size_of::<WireRequest>() as isize
        );
    }

    fn receive_response_only(client: RawFd) -> WireResponse {
        let mut response = MaybeUninit::<WireResponse>::zeroed();
        assert_eq!(
            unsafe {
                libc::recv(
                    client,
                    response.as_mut_ptr().cast(),
                    size_of::<WireResponse>(),
                    0,
                )
            },
            size_of::<WireResponse>() as isize
        );
        unsafe { response.assume_init() }
    }

    fn wait_until_missing(path: &Path) {
        for _ in 0..100 {
            if fs::symlink_metadata(path)
                .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
            {
                return;
            }
            thread::sleep(Duration::from_millis(5));
        }
        panic!(
            "pending publication was not rolled back: {}",
            path.display()
        );
    }

    #[test]
    fn routed_publish_and_retire_are_fd_relative_under_exact_lease() {
        let fixture = Fixture::new();
        let (controller, darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        assert!(darlingserver.as_raw_fd() >= 0);
        let publish = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
        assert_eq!(publish.status, 0);
        assert_eq!(publish.has_fd, 1);
        assert!(fixture.root.join("var/run/shellspawn.sock").exists());
        let retire = request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce);
        assert_eq!(retire.status, 0);
        assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
        controller.finish().unwrap();
        assert!(!fixture.root.join(".init.pid").exists());
        assert!(!fixture.root.join(".darlingserver.sock").exists());
    }

    fn response_path(response: &WireResponse) -> String {
        let length = usize::from(response.path_len);
        assert!(length > 0 && length < response.path.len());
        assert_eq!(response.path[length], 0);
        std::str::from_utf8(&response.path[..length])
            .unwrap()
            .to_owned()
    }

    #[test]
    fn per_user_launchd_uses_retained_dynamic_directory_and_exact_retirement() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let published = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        assert_eq!(published.status, 0);
        assert_eq!(published.has_fd, 1);
        let guest_path = response_path(&published);
        assert!(guest_path.starts_with("/private/var/tmp/launchd-"));
        assert!(guest_path.ends_with("/sock"));
        let endpoint = fixture.root.join(guest_path.trim_start_matches('/'));
        let directory = endpoint.parent().unwrap().to_owned();
        let endpoint_state = fs::symlink_metadata(&endpoint).unwrap();
        assert!(endpoint_state.file_type().is_socket());
        assert_eq!(endpoint_state.permissions().mode() & 0o777, 0o600);
        let directory_state = fs::symlink_metadata(&directory).unwrap();
        assert!(directory_state.is_dir());
        assert_eq!(directory_state.permissions().mode() & 0o777, 0o700);

        let duplicate = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        assert_eq!(duplicate.status, -1);
        assert!(endpoint.exists());

        let retired = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_RETIRE,
            controller.nonce,
        );
        assert_eq!(retired.status, 0);
        assert!(!endpoint.exists());
        assert!(!directory.exists());
        controller.finish().unwrap();
    }

    #[test]
    fn per_user_dynamic_directory_partial_creation_is_rolled_back() {
        let fixture = Fixture::new();
        let mut authority = SessionAuthority::acquire(&fixture.root, 4242).unwrap();
        authority.dynamic_fault = Some(DynamicPublicationFault::AfterDirectoryCreate);
        let peer = process_identity(unsafe { libc::getpid() }).unwrap().0;
        assert!(matches!(
            authority.publish_key(EndpointKey::PerUser(peer)),
            Err(CohortError::Io("fault(after dynamic directory create)", _))
        ));
        let parent = fixture.root.join("private/var/tmp");
        assert!(fs::read_dir(parent).unwrap().next().is_none());
        authority.cleanup_all().unwrap();
    }

    #[test]
    fn per_user_endpoint_replacement_is_preserved_fail_closed() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let published = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        let endpoint = fixture
            .root
            .join(response_path(&published).trim_start_matches('/'));
        let saved = endpoint.with_extension("saved");
        fs::rename(&endpoint, &saved).unwrap();
        fs::write(&endpoint, b"replacement").unwrap();
        let refused = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_RETIRE,
            controller.nonce,
        );
        assert_eq!(refused.status, -1);
        assert_eq!(fs::read(&endpoint).unwrap(), b"replacement");
        fs::remove_file(&endpoint).unwrap();
        fs::rename(&saved, &endpoint).unwrap();
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::PerUserLaunchd,
                OPERATION_RETIRE,
                controller.nonce,
            )
            .status,
            0
        );
        controller.finish().unwrap();
    }

    #[test]
    fn per_user_directory_replacement_is_preserved_fail_closed() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let published = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        let endpoint = fixture
            .root
            .join(response_path(&published).trim_start_matches('/'));
        let directory = endpoint.parent().unwrap().to_owned();
        let saved = directory.with_extension("saved");
        fs::rename(&directory, &saved).unwrap();
        fs::create_dir(&directory).unwrap();
        fs::write(directory.join("replacement"), b"preserve").unwrap();
        let refused = request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_RETIRE,
            controller.nonce,
        );
        assert_eq!(refused.status, -1);
        assert_eq!(
            fs::read(directory.join("replacement")).unwrap(),
            b"preserve"
        );
        fs::remove_file(directory.join("replacement")).unwrap();
        fs::remove_dir(&directory).unwrap();
        fs::rename(&saved, &directory).unwrap();
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::PerUserLaunchd,
                OPERATION_RETIRE,
                controller.nonce,
            )
            .status,
            0
        );
        controller.finish().unwrap();
    }

    #[test]
    fn per_user_pending_eof_removes_socket_and_dynamic_directory() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let client = connect(&controller.control_test_path);
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::PerUserLaunchd,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        let pending = receive_response_only(client.as_raw_fd());
        let endpoint = fixture
            .root
            .join(response_path(&pending).trim_start_matches('/'));
        let directory = endpoint.parent().unwrap().to_owned();
        drop(client);
        wait_until_missing(&endpoint);
        wait_until_missing(&directory);
        controller.finish().unwrap();
    }

    #[test]
    fn symlinked_dynamic_ancestor_is_rejected_before_mutation() {
        let fixture = Fixture::new();
        let outside = fixture.root.join("outside");
        fs::create_dir(&outside).unwrap();
        std::os::unix::fs::symlink(&outside, fixture.root.join("private")).unwrap();
        assert!(SessionAuthority::acquire(&fixture.root, 4242).is_err());
        assert!(fs::read_dir(outside).unwrap().next().is_none());
    }

    #[test]
    fn pending_publication_rolls_back_on_client_eof_before_adoption() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let client = connect(&controller.control_test_path);
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        let response = receive_response_only(client.as_raw_fd());
        assert_eq!(response.status, 0);
        drop(client);
        wait_until_missing(&fixture.root.join("var/run/shellspawn.sock"));
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::Shellspawn,
                OPERATION_PUBLISH,
                controller.nonce,
            )
            .status,
            0
        );
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::Shellspawn,
                OPERATION_RETIRE,
                controller.nonce,
            )
            .status,
            0
        );
        controller.finish().unwrap();
    }

    #[test]
    fn pending_publication_rolls_back_on_explicit_adoption_abort() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let client = connect(&controller.control_test_path);
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        assert_eq!(receive_response_only(client.as_raw_fd()).status, 0);
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::Shellspawn,
            OPERATION_ABORT,
            controller.nonce,
        );
        drop(client);
        wait_until_missing(&fixture.root.join("var/run/shellspawn.sock"));
        controller.finish().unwrap();
    }

    #[test]
    fn response_delivery_failure_cannot_leave_pending_publication() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let client = connect(&controller.control_test_path);
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        unsafe { libc::shutdown(client.as_raw_fd(), libc::SHUT_RDWR) };
        drop(client);
        wait_until_missing(&fixture.root.join("var/run/shellspawn.sock"));
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::Shellspawn,
                OPERATION_PUBLISH,
                controller.nonce,
            )
            .status,
            0
        );
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::Shellspawn,
                OPERATION_RETIRE,
                controller.nonce,
            )
            .status,
            0
        );
        controller.finish().unwrap();
    }

    #[test]
    fn final_ack_delivery_failure_preserves_committed_publication() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let client = connect(&controller.control_test_path);
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        );
        assert_eq!(receive_response_only(client.as_raw_fd()).status, 0);

        // Make the final acknowledgement undeliverable while preserving the
        // write side used for the irrevocable COMMIT.
        assert_eq!(
            unsafe { libc::shutdown(client.as_raw_fd(), libc::SHUT_RD) },
            0
        );
        send_request_only(
            client.as_raw_fd(),
            CohortEndpoint::Shellspawn,
            OPERATION_COMMIT,
            controller.nonce,
        );
        drop(client);

        let endpoint = fixture.root.join("var/run/shellspawn.sock");
        assert!(endpoint.exists());
        assert_ne!(
            request(
                &controller,
                CohortEndpoint::Shellspawn,
                OPERATION_PUBLISH,
                controller.nonce,
            )
            .status,
            0,
            "a lost final ACK must not make the committed endpoint publishable again"
        );
        assert_eq!(
            request(
                &controller,
                CohortEndpoint::Shellspawn,
                OPERATION_RETIRE,
                controller.nonce,
            )
            .status,
            0
        );
        controller.finish().unwrap();
    }

    #[test]
    fn wrong_nonce_and_unknown_endpoint_fail_closed() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let wrong = request(&controller, CohortEndpoint::Shellspawn, 1, [0x55; 32]);
        assert_ne!(wrong.status, 0);
        assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
        let unknown_operation = request(
            &controller,
            CohortEndpoint::Shellspawn,
            99,
            controller.nonce,
        );
        assert_ne!(unknown_operation.status, 0);
        controller.finish().unwrap();
    }

    #[test]
    fn malformed_request_flood_does_not_consume_authority_lifetime() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        for _ in 0..=MAX_REJECTED_REQUESTS_PER_SLICE {
            let rejected = request(
                &controller,
                CohortEndpoint::Shellspawn,
                1,
                [0x55; NONCE_BYTES],
            );
            assert_ne!(rejected.status, 0);
        }
        let published = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
        assert_eq!(published.status, 0);
        assert_eq!(published.has_fd, 1);
        assert_eq!(
            request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce,).status,
            0
        );
        controller.finish().unwrap();
    }

    #[test]
    fn dead_endpoint_owner_transitions_to_exact_republication() {
        let fixture = Fixture::new();
        let mut authority = SessionAuthority::acquire(&fixture.root, 4242).unwrap();
        let first_listener = authority.publish(CohortEndpoint::Shellspawn).unwrap();
        let first_identity = identity(first_listener.as_raw_fd()).unwrap();
        let mut owner = Command::new("/bin/sh")
            .args(["-c", "exec sleep 60"])
            .spawn()
            .unwrap();
        let owner_pid = owner.id() as libc::pid_t;
        let owner_identity = process_identity(owner_pid).unwrap().0;
        let owner_pidfd = unsafe { libc::syscall(libc::SYS_pidfd_open, owner_pid, 0) as c_int };
        assert!(owner_pidfd >= 0);
        authority.endpoint_owners.insert(
            EndpointKey::Static(CohortEndpoint::Shellspawn),
            PeerAuthority {
                identity: owner_identity,
                _process: unsafe { OwnedFd::from_raw_fd(owner_pidfd) },
            },
        );
        owner.kill().unwrap();
        owner.wait().unwrap();
        assert_eq!(
            owner_process_state(
                authority
                    .endpoint_owners
                    .get(&EndpointKey::Static(CohortEndpoint::Shellspawn))
                    .unwrap()
                    ._process
                    .as_raw_fd()
            )
            .unwrap(),
            OwnerProcessState::Gone
        );
        drop(first_listener);
        let second_listener = authority.publish(CohortEndpoint::Shellspawn).unwrap();
        let second_identity = identity(second_listener.as_raw_fd()).unwrap();
        assert_ne!(first_identity.inode_key(), second_identity.inode_key());
        assert!(!authority
            .endpoint_owners
            .contains_key(&EndpointKey::Static(CohortEndpoint::Shellspawn)));
        authority.cleanup_all().unwrap();
    }

    #[test]
    fn lock_contender_cannot_mutate_existing_inode_before_lease() {
        let fixture = Fixture::new();
        let authority = SessionAuthority::acquire(&fixture.root, 4242).unwrap();
        let lock_path = fixture.root.join(".lifecycle.lock");
        let before = fs::metadata(&lock_path).unwrap();
        let contender_root = fixture.root.clone();
        let contender = thread::spawn(move || SessionAuthority::acquire(&contender_root, 4343));
        assert!(matches!(
            contender.join().unwrap(),
            Err(CohortError::LockBusy)
        ));
        let after = fs::metadata(&lock_path).unwrap();
        assert_eq!(
            (
                before.dev(),
                before.ino(),
                before.ctime(),
                before.ctime_nsec()
            ),
            (after.dev(), after.ino(), after.ctime(), after.ctime_nsec())
        );
        drop(authority);
    }

    #[test]
    fn retained_prefix_state_rejects_in_place_mutation() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let state = fixture.root.join(".darling-prefix-state-v2");
        let original = fs::read(&state).unwrap();
        let before = fs::metadata(&state).unwrap();
        fs::write(&state, b"privileged-eunion\n").unwrap();
        let after = fs::metadata(&state).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        let rejected = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
        assert_ne!(rejected.status, 0);
        assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
        fs::write(&state, original).unwrap();
        controller.finish().unwrap();
    }

    #[test]
    fn retained_prefix_fd_does_not_reopen_proc_path() {
        let fixture = Fixture::new();
        let prefix = open_prefix(&fixture.root).unwrap();
        let proc_argument = format!("/proc/self/fd/{}", prefix.as_raw_fd());
        let authority =
            SessionAuthority::acquire_from_fd(prefix.as_raw_fd(), proc_argument.as_bytes(), 4242)
                .unwrap();
        assert_eq!(authority.prefix_argument, proc_argument.as_bytes());
        drop(authority);
    }

    #[test]
    fn controller_creates_missing_endpoint_parents_under_retained_lease() {
        let fixture = Fixture::new();
        fs::remove_dir_all(fixture.root.join("var")).unwrap();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let run = fs::metadata(fixture.root.join("var/run")).unwrap();
        let launchd = fs::metadata(fixture.root.join("var/tmp/launchd")).unwrap();
        assert_eq!(run.mode() & 0o7777, 0o755);
        assert_eq!(launchd.mode() & 0o7777, 0o700);
        controller.finish().unwrap();
    }

    #[test]
    fn retained_init_pid_replacement_is_preserved_on_finish() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let init_pid = fixture.root.join(".init.pid");
        fs::rename(&init_pid, fixture.root.join(".init.pid.original")).unwrap();
        fs::write(&init_pid, b"999999\n").unwrap();
        fs::set_permissions(&init_pid, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(controller.finish().is_err());
        assert_eq!(fs::read(&init_pid).unwrap(), b"999999\n");
    }

    #[test]
    fn connected_client_without_request_has_bounded_unwind() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let _stalled = connect(&controller.control_test_path);
        thread::sleep(Duration::from_millis(20));
        let started = Instant::now();
        controller.finish().unwrap();
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn pre_thread_authority_drop_rolls_back_published_namespace() {
        let fixture = Fixture::new();
        {
            let mut authority = SessionAuthority::acquire(&fixture.root, 4242).unwrap();
            drop(authority.publish(CohortEndpoint::DarlingServer).unwrap());
            drop(authority.publish(CohortEndpoint::Control).unwrap());
            assert!(fixture.root.join(".init.pid").exists());
            assert!(fixture.root.join(".darlingserver.sock").exists());
            assert!(fixture.root.join(".lc-v1.sock").exists());
        }
        assert!(!fixture.root.join(".init.pid").exists());
        assert!(!fixture.root.join(".darlingserver.sock").exists());
        assert!(!fixture.root.join(".lc-v1.sock").exists());
    }

    #[test]
    fn split_lock_is_rejected_before_endpoint_mutation() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let original = fs::metadata(fixture.root.join(".lifecycle.lock")).unwrap();
        fs::rename(
            fixture.root.join(".lifecycle.lock"),
            fixture.root.join(".lifecycle.lock.old"),
        )
        .unwrap();
        fs::write(fixture.root.join(".lifecycle.lock"), b"").unwrap();
        fs::set_permissions(
            fixture.root.join(".lifecycle.lock"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        let replacement = fs::metadata(fixture.root.join(".lifecycle.lock")).unwrap();
        assert_ne!(
            (original.dev(), original.ino()),
            (replacement.dev(), replacement.ino())
        );
        let rejected = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
        assert_ne!(rejected.status, 0);
        assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
        drop(controller);
    }

    #[test]
    fn endpoint_replacement_is_preserved_and_retirement_fails_closed() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        assert_eq!(
            request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce).status,
            0
        );
        let endpoint = fixture.root.join("var/run/shellspawn.sock");
        fs::rename(&endpoint, endpoint.with_extension("original")).unwrap();
        fs::write(&endpoint, b"replacement").unwrap();
        let rejected = request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce);
        assert_ne!(rejected.status, 0);
        assert_eq!(fs::read(&endpoint).unwrap(), b"replacement");
        drop(controller);
    }

    #[test]
    fn retained_prefix_fd_prevents_path_replacement_redirection() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let retained = fixture.root.with_extension("retained");
        fs::rename(&fixture.root, &retained).unwrap();
        fs::create_dir(&fixture.root).unwrap();
        fs::create_dir_all(fixture.root.join("var/run")).unwrap();
        fs::create_dir_all(fixture.root.join("var/tmp/launchd")).unwrap();

        assert!(!fixture.root.join(".lc-v1.sock").exists());
        assert!(!retained.join("var/run/shellspawn.sock").exists());
        assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
        fs::remove_dir_all(&fixture.root).unwrap();
        fs::rename(retained, &fixture.root).unwrap();
        assert_eq!(
            request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce).status,
            0
        );
        controller.finish().unwrap();
    }
}
