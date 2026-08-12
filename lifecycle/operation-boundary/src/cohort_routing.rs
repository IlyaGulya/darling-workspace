//! Rust-owned routing boundary for the first Rootless namespace-writer cohort.
//!
//! The controller retains the prefix directory and the exact
//! `.lifecycle.lock` open-file description for its whole lifetime.  Product
//! processes never receive pathname mutation authority: they request one of
//! the finite endpoint kinds and receive only the already-bound listener via
//! `SCM_RIGHTS`.  The v1 threat model is deliberately cooperative; writers
//! outside this cohort keep global routing disabled.

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

const MARKER_NAME: &[u8] = b".darling-runtime-mode-v1";
const MARKER_VALUE: &[u8] = b"DARLING_RUNTIME_MODE_V1=rootless-eunion\n";
const LOCK_NAME: &[u8] = b".lifecycle.lock";
const INIT_PID_NAME: &[u8] = b".init.pid";
const PROTOCOL_MAGIC: [u8; 8] = *b"DLCOHR1\0";
const PROTOCOL_VERSION: u16 = 1;
const CONTROL_NAME_CAPACITY: usize = 80;
const NONCE_BYTES: usize = 32;
const NONCE_HEX_BYTES: usize = NONCE_BYTES * 2;
const LOCK_TIMEOUT: Duration = Duration::from_millis(250);
const REQUEST_TIMEOUT_MS: c_int = 250;
#[cfg(not(test))]
const CONTROLLER_EXIT_TIMEOUT_MS: c_int = 1_000;
const MAX_REJECTED_REQUESTS_PER_SLICE: usize = 128;
const SO_PEERPIDFD: c_int = 77;
const CONTROL_GUEST_PATH: &[u8] = b"/private/var/run/.darling-lifecycle-controller-v1.sock";
const CONTROL_NAME: &[u8] = b".darling-lifecycle-controller-v1.sock";
const SHELLSPAWN_PARENT: &[&[u8]] = &[b"private", b"var", b"run"];
const LAUNCHD_PARENT: &[&[u8]] = &[b"private", b"var", b"tmp", b"launchd"];

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
#[repr(u16)]
pub enum CohortEndpoint {
    DarlingServer = 1,
    Shellspawn = 2,
    Launchd = 3,
    #[doc(hidden)]
    Control = 4,
}

impl CohortEndpoint {
    fn from_wire(value: u16) -> Option<Self> {
        match value {
            1 => Some(Self::DarlingServer),
            2 => Some(Self::Shellspawn),
            3 => Some(Self::Launchd),
            _ => None,
        }
    }

    fn spec(self) -> EndpointSpec {
        match self {
            Self::DarlingServer => EndpointSpec {
                parent: &[],
                name: b".darlingserver.sock",
                socket_type: libc::SOCK_DGRAM,
                nonblocking: true,
                listen_backlog: None,
                mode: 0o775,
            },
            Self::Shellspawn => EndpointSpec {
                parent: SHELLSPAWN_PARENT,
                name: b"shellspawn.sock",
                socket_type: libc::SOCK_STREAM,
                nonblocking: false,
                listen_backlog: Some(16_384),
                mode: 0o600,
            },
            Self::Launchd => EndpointSpec {
                parent: LAUNCHD_PARENT,
                name: b"sock",
                socket_type: libc::SOCK_STREAM,
                nonblocking: false,
                listen_backlog: Some(libc::SOMAXCONN),
                mode: 0o600,
            },
            Self::Control => EndpointSpec {
                parent: SHELLSPAWN_PARENT,
                name: CONTROL_NAME,
                socket_type: libc::SOCK_SEQPACKET,
                nonblocking: true,
                listen_backlog: Some(16),
                mode: 0o600,
            },
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

fn acquire_marker(prefix: RawFd) -> Result<RetainedFile, CohortError> {
    let marker = openat(
        prefix,
        MARKER_NAME,
        libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        0,
    )?;
    let expected = identity(marker.as_raw_fd())?;
    validate_owned(expected, libc::S_IFREG, Some(0o600))?;
    if named_identity(prefix, MARKER_NAME)? != Some(expected)
        || read_bounded(marker.as_raw_fd(), 64)? != MARKER_VALUE
    {
        return Err(CohortError::Identity("runtime marker"));
    }
    Ok(RetainedFile {
        object: marker,
        identity: expected,
    })
}

fn revalidate_marker(prefix: RawFd, marker: &RetainedFile) -> Result<(), CohortError> {
    if identity(marker.object.as_raw_fd())? != marker.identity
        || named_identity(prefix, MARKER_NAME)? != Some(marker.identity)
        || read_bounded(marker.object.as_raw_fd(), 64)? != MARKER_VALUE
    {
        return Err(CohortError::Identity("runtime marker"));
    }
    Ok(())
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
    name: &'static [u8],
}

#[derive(Debug)]
struct RetainedFile {
    object: OwnedFd,
    identity: FileIdentity,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PeerIdentity {
    pid: libc::pid_t,
    starttime: u64,
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
    marker: RetainedFile,
    init_pid: Option<RetainedFile>,
    endpoints: BTreeMap<CohortEndpoint, PublishedEndpoint>,
    endpoint_owners: BTreeMap<CohortEndpoint, PeerAuthority>,
    session_root_pid: libc::pid_t,
    prefix_argument: Vec<u8>,
    cleanup_on_drop: bool,
    #[cfg(test)]
    allow_test_peer: bool,
}

impl SessionAuthority {
    fn acquire(prefix_path: &Path, init_pid: libc::pid_t) -> Result<Self, CohortError> {
        if init_pid <= 0 {
            return Err(CohortError::Protocol("invalid init pid"));
        }
        let prefix = open_prefix(prefix_path)?;
        let prefix_identity = identity(prefix.as_raw_fd())?;
        let (lock, lock_identity) = acquire_lock(prefix.as_raw_fd())?;
        let marker = acquire_marker(prefix.as_raw_fd())?;
        let mut authority = Self {
            prefix,
            prefix_identity,
            lock,
            lock_identity,
            marker,
            init_pid: None,
            endpoints: BTreeMap::new(),
            endpoint_owners: BTreeMap::new(),
            session_root_pid: init_pid,
            prefix_argument: prefix_path.as_os_str().as_bytes().to_vec(),
            cleanup_on_drop: true,
            #[cfg(test)]
            allow_test_peer: false,
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
        revalidate_marker(self.prefix.as_raw_fd(), &self.marker)
    }

    #[cfg(not(test))]
    fn disarm_drop_cleanup(&mut self) {
        self.cleanup_on_drop = false;
    }

    #[cfg(not(test))]
    fn retained_fds(&self) -> Vec<RawFd> {
        let mut fds = vec![
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.marker.object.as_raw_fd(),
        ];
        if let Some(init_pid) = self.init_pid.as_ref() {
            fds.push(init_pid.object.as_raw_fd());
        }
        for endpoint in self.endpoints.values() {
            fds.extend([endpoint.parent.as_raw_fd(), endpoint.object.as_raw_fd()]);
        }
        for owner in self.endpoint_owners.values() {
            fds.push(owner._process.as_raw_fd());
        }
        fds
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
        kind: CohortEndpoint,
    ) -> Result<OwnerDeathTransition, CohortError> {
        if !self.endpoints.contains_key(&kind) {
            if self.endpoint_owners.contains_key(&kind) {
                return Err(CohortError::Identity("endpoint owner without endpoint"));
            }
            return Ok(OwnerDeathTransition::Fresh);
        }
        let Some(owner) = self.endpoint_owners.get(&kind) else {
            return Ok(OwnerDeathTransition::RetainedAlive);
        };
        match owner_process_state(owner._process.as_raw_fd())? {
            OwnerProcessState::Alive => Ok(OwnerDeathTransition::RetainedAlive),
            OwnerProcessState::Gone => {
                // The retained pidfd proves owner death without consulting a
                // reusable numeric PID.  Retirement still requires the exact
                // retained endpoint inode under the session lease.
                self.retire(kind)?;
                Ok(OwnerDeathTransition::RetiredGone)
            }
        }
    }

    fn publish(&mut self, kind: CohortEndpoint) -> Result<OwnedFd, CohortError> {
        self.revalidate()?;
        if self.prepare_publication(kind)? == OwnerDeathTransition::RetainedAlive {
            return Err(CohortError::EndpointExists);
        }
        let spec = kind.spec();
        let parent = open_directory_chain(self.prefix.as_raw_fd(), spec.parent)?;
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
                return Err(error);
            }
        };
        self.endpoints.insert(
            kind,
            PublishedEndpoint {
                parent,
                parent_identity,
                object,
                identity: endpoint_identity,
                name: spec.name,
            },
        );
        Ok(socket)
    }

    fn authorize_peer(
        &self,
        kind: CohortEndpoint,
        operation: u16,
        peer: PeerAuthority,
    ) -> Result<PeerAuthority, CohortError> {
        #[cfg(test)]
        if self.allow_test_peer {
            return Ok(peer);
        }
        let (identity, parent) = process_identity(peer.identity.pid)?;
        if identity != peer.identity {
            return Err(CohortError::Protocol("peer identity drift"));
        }
        if operation == 2 {
            let owner = self
                .endpoint_owners
                .get(&kind)
                .ok_or(CohortError::Protocol("endpoint owner"))?;
            return if owner.identity == peer.identity
                && owner_process_state(owner._process.as_raw_fd())? == OwnerProcessState::Alive
            {
                Ok(peer)
            } else {
                Err(CohortError::Protocol("endpoint owner"))
            };
        }
        match kind {
            CohortEndpoint::Launchd => {
                let argv = process_argv(peer.identity.pid)?;
                if parent != self.session_root_pid
                    || argv.len() != 3
                    || argv0_basename(&argv) != b"vchroot"
                    || argv[1] != self.prefix_argument
                    || argv[2] != b"/sbin/launchd"
                {
                    return Err(CohortError::Protocol("launchd peer identity"));
                }
            }
            CohortEndpoint::Shellspawn => {
                let launchd = self
                    .endpoint_owners
                    .get(&CohortEndpoint::Launchd)
                    .ok_or(CohortError::Protocol("launchd authority missing"))?;
                let argv = process_argv(peer.identity.pid)?;
                if argv.len() != 1
                    || argv0_basename(&argv) != b"shellspawn"
                    || parent != launchd.identity.pid
                {
                    return Err(CohortError::Protocol("shellspawn peer identity"));
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
        Ok(peer)
    }

    fn retire(&mut self, kind: CohortEndpoint) -> Result<(), CohortError> {
        self.revalidate()?;
        let endpoint = self
            .endpoints
            .get(&kind)
            .ok_or(CohortError::EndpointMissing)?;
        if identity(endpoint.parent.as_raw_fd())? != endpoint.parent_identity
            || identity(endpoint.object.as_raw_fd())? != endpoint.identity
            || named_identity(endpoint.parent.as_raw_fd(), endpoint.name)?
                != Some(endpoint.identity)
        {
            return Err(CohortError::Identity("endpoint replacement"));
        }
        let name = component(endpoint.name)?;
        if unsafe { libc::unlinkat(endpoint.parent.as_raw_fd(), name.as_ptr(), 0) } < 0 {
            return Err(io_error("unlinkat(endpoint)"));
        }
        self.endpoints.remove(&kind);
        self.endpoint_owners.remove(&kind);
        Ok(())
    }

    fn cleanup_all(&mut self) -> Result<(), CohortError> {
        let mut first_error = None;
        for kind in [
            CohortEndpoint::Shellspawn,
            CohortEndpoint::Launchd,
            CohortEndpoint::DarlingServer,
            CohortEndpoint::Control,
        ] {
            if self.endpoints.contains_key(&kind) {
                if let Err(error) = self.retire(kind) {
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
    if request.operation != 1 && request.operation != 2 {
        return Err(CohortError::Protocol("operation"));
    }
    let peer_process = peer_pidfd(client, credentials.pid)?;
    let (peer_identity, _) = process_identity(credentials.pid)?;
    if owner_process_state(peer_process.as_raw_fd())? != OwnerProcessState::Alive {
        return Err(CohortError::Protocol("peer exited during identity bind"));
    }
    let peer = authority.authorize_peer(
        kind,
        request.operation,
        PeerAuthority {
            identity: peer_identity,
            _process: peer_process,
        },
    )?;
    let (result, passed_fd, observed) = match request.operation {
        1 => match authority.publish(kind) {
            Ok(socket) => {
                match identity(socket.as_raw_fd()) {
                    Ok(socket_identity) => {
                        authority.endpoint_owners.insert(kind, peer);
                        (Ok(()), Some(socket), Some(socket_identity))
                    }
                    Err(error) => {
                        // Publication has already mutated the namespace.  Do
                        // not report failure while retaining an unowned
                        // endpoint if the SCM_RIGHTS witness cannot be formed.
                        let _ = authority.retire(kind);
                        (Err(error), None, None)
                    }
                }
            }
            Err(error) => (Err(error), None, None),
        },
        2 => (authority.retire(kind), None, None),
        _ => unreachable!("operation was validated before authorization"),
    };
    let status = if result.is_ok() { 0 } else { -1 };
    if let Err(error) = result.as_ref() {
        eprintln!("lifecycle cohort operation refused: {error}");
    }
    let response = WireResponse {
        magic: PROTOCOL_MAGIC,
        version: PROTOCOL_VERSION,
        status,
        endpoint: request.endpoint,
        has_fd: u16::from(passed_fd.is_some()),
        device: observed.map_or(0, |value| value.device),
        inode: observed.map_or(0, |value| value.inode),
    };
    send_response(client, response, passed_fd.as_ref().map(AsRawFd::as_raw_fd))?;
    // Operation failures are represented by the typed response.  Returning
    // Ok here prevents the server loop from emitting a second response.
    Ok(())
}

fn server_loop(
    mut authority: SessionAuthority,
    listener: OwnedFd,
    shutdown: OwnedFd,
    parent_watch: Option<OwnedFd>,
    nonce: [u8; NONCE_BYTES],
) -> (SessionAuthority, Result<(), CohortError>) {
    let mut rejected_requests = 0usize;
    let result = loop {
        let mut pollfds = [
            libc::pollfd {
                fd: listener.as_raw_fd(),
                events: libc::POLLIN,
                revents: 0,
            },
            libc::pollfd {
                fd: shutdown.as_raw_fd(),
                events: libc::POLLIN,
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
            break Err(io_error("poll(controller)"));
        }
        if pollfds[1].revents & libc::POLLIN != 0 {
            break Ok(());
        }
        if pollfds[2].revents & libc::POLLIN != 0 {
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
            let _ = send_response(
                client.as_raw_fd(),
                WireResponse {
                    magic: PROTOCOL_MAGIC,
                    version: PROTOCOL_VERSION,
                    status: -1,
                    endpoint: 0,
                    has_fd: 0,
                    device: 0,
                    inode: 0,
                },
                None,
            );
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
    (authority, result)
}

#[repr(C)]
pub struct CohortBootstrap {
    pub darlingserver_fd: c_int,
    pub control_name_len: u16,
    pub reserved: u16,
    pub control_name: [u8; CONTROL_NAME_CAPACITY],
    pub nonce_hex: [u8; NONCE_HEX_BYTES],
}

#[cfg(not(test))]
struct ProcessWorker {
    pid: libc::pid_t,
    pidfd: OwnedFd,
}

#[cfg(not(test))]
fn pidfd_open(pid: libc::pid_t) -> Result<OwnedFd, CohortError> {
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) as c_int };
    if fd < 0 {
        return Err(io_error("pidfd_open(controller)"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
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
    #[cfg(test)]
    thread: Option<JoinHandle<(SessionAuthority, Result<(), CohortError>)>>,
    #[cfg(not(test))]
    process: Option<ProcessWorker>,
    control_name: Vec<u8>,
    nonce: [u8; NONCE_BYTES],
    #[cfg(test)]
    control_test_path: std::path::PathBuf,
}

impl CohortController {
    pub fn start(prefix: &Path, init_pid: libc::pid_t) -> Result<(Self, OwnedFd), CohortError> {
        Self::start_inner(prefix, init_pid, false)
    }

    fn start_inner(
        prefix: &Path,
        init_pid: libc::pid_t,
        allow_test_peer: bool,
    ) -> Result<(Self, OwnedFd), CohortError> {
        let mut authority = SessionAuthority::acquire(prefix, init_pid)?;
        #[cfg(test)]
        {
            authority.allow_test_peer = allow_test_peer;
        }
        #[cfg(not(test))]
        let _ = allow_test_peer;
        let darlingserver = authority.publish(CohortEndpoint::DarlingServer)?;
        let nonce = random_nonce()?;
        let name = CONTROL_GUEST_PATH.to_vec();
        let listener = authority.publish(CohortEndpoint::Control)?;
        let shutdown = unsafe { libc::eventfd(0, libc::EFD_CLOEXEC | libc::EFD_NONBLOCK) };
        if shutdown < 0 {
            return Err(io_error("eventfd"));
        }
        let shutdown = unsafe { OwnedFd::from_raw_fd(shutdown) };

        #[cfg(test)]
        let controller = {
            let thread_shutdown = duplicate(shutdown.as_raw_fd(), true)?;
            let thread = thread::Builder::new()
                .name("darling-lifecycle-cohort".to_string())
                .spawn(move || server_loop(authority, listener, thread_shutdown, None, nonce))
                .map_err(|error| CohortError::Io("spawn(controller)", error))?;
            Self {
                shutdown,
                thread: Some(thread),
                control_name: name,
                nonce,
                #[cfg(test)]
                control_test_path: prefix
                    .join("private/var/run")
                    .join(std::ffi::OsStr::from_bytes(CONTROL_NAME)),
            }
        };

        #[cfg(not(test))]
        let controller = {
            let parent_pid = unsafe { libc::getpid() };
            let parent_watch = pidfd_open(parent_pid)?;
            let child_shutdown = duplicate(shutdown.as_raw_fd(), true)?;
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
                unsafe { libc::close(darlingserver.as_raw_fd()) };
                if unsafe { libc::setsid() } < 0 {
                    let ready_status: i32 = -1;
                    let _ = write_all(ready_child.as_raw_fd(), &ready_status.to_ne_bytes());
                    let _ = authority.cleanup_all();
                    unsafe { libc::_exit(1) };
                }
                let mut allowed = authority.retained_fds();
                allowed.extend([
                    listener.as_raw_fd(),
                    child_shutdown.as_raw_fd(),
                    parent_watch.as_raw_fd(),
                    ready_child.as_raw_fd(),
                ]);
                let prepared = close_unowned_child_fds(&allowed);
                let ready_status: i32 = if prepared.is_ok() { 0 } else { -1 };
                let _ = write_all(ready_child.as_raw_fd(), &ready_status.to_ne_bytes());
                drop(ready_child);
                if prepared.is_err() {
                    let _ = authority.cleanup_all();
                    unsafe { libc::_exit(1) };
                }
                let (mut authority, loop_result) = server_loop(
                    authority,
                    listener,
                    child_shutdown,
                    Some(parent_watch),
                    nonce,
                );
                let cleanup_result = authority.cleanup_all();
                let status = i32::from(loop_result.is_err() || cleanup_result.is_err());
                unsafe { libc::_exit(status) };
            }
            drop(ready_child);
            authority.disarm_drop_cleanup();
            drop(authority);
            drop(listener);
            drop(child_shutdown);
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
                process: Some(process),
                control_name: name,
                nonce,
            }
        };

        Ok((controller, darlingserver))
    }

    #[cfg(test)]
    fn start_for_test(
        prefix: &Path,
        init_pid: libc::pid_t,
    ) -> Result<(Self, OwnedFd), CohortError> {
        Self::start_inner(prefix, init_pid, true)
    }

    pub fn control_name(&self) -> &[u8] {
        &self.control_name
    }

    pub fn nonce_hex(&self) -> [u8; NONCE_HEX_BYTES] {
        nonce_hex(&self.nonce)
    }

    pub fn finish(mut self) -> Result<(), CohortError> {
        let value: u64 = 1;
        write_all(self.shutdown.as_raw_fd(), &value.to_ne_bytes())?;
        #[cfg(test)]
        {
            let (mut authority, loop_result) = self
                .thread
                .take()
                .ok_or(CohortError::Thread)?
                .join()
                .map_err(|_| CohortError::Thread)?;
            let cleanup_result = authority.cleanup_all();
            cleanup_result?;
            loop_result
        }
        #[cfg(not(test))]
        {
            wait_worker(self.process.take().ok_or(CohortError::Process)?)
        }
    }
}

impl Drop for CohortController {
    fn drop(&mut self) {
        #[cfg(test)]
        if self.thread.is_some() {
            let value: u64 = 1;
            let _ = write_all(self.shutdown.as_raw_fd(), &value.to_ne_bytes());
            if let Some(thread) = self.thread.take() {
                if let Ok((mut authority, _)) = thread.join() {
                    let _ = authority.cleanup_all();
                }
            }
        }
        #[cfg(not(test))]
        if let Some(process) = self.process.take() {
            let value: u64 = 1;
            let _ = write_all(self.shutdown.as_raw_fd(), &value.to_ne_bytes());
            let _ = wait_worker(process);
        }
    }
}

#[no_mangle]
/// Start one retained first-cohort controller.
///
/// # Safety
///
/// `prefix` must point to a live NUL-terminated path for the duration of the
/// call and `output` must point to writable `CohortBootstrap` storage. The
/// returned pointer must be consumed exactly once by
/// [`darling_lifecycle_cohort_finish`].
pub unsafe extern "C" fn darling_lifecycle_cohort_start(
    prefix: *const c_char,
    init_pid: libc::pid_t,
    output: *mut CohortBootstrap,
) -> *mut CohortController {
    if prefix.is_null() || output.is_null() {
        return ptr::null_mut();
    }
    let prefix = CStr::from_ptr(prefix);
    let path = Path::new(std::ffi::OsStr::from_bytes(prefix.to_bytes()));
    let Ok((controller, darlingserver)) = CohortController::start(path, init_pid) else {
        return ptr::null_mut();
    };
    if controller.control_name.len() > CONTROL_NAME_CAPACITY {
        return ptr::null_mut();
    }
    let mut bootstrap = CohortBootstrap {
        darlingserver_fd: darlingserver.into_raw_fd(),
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
    let controller = Box::from_raw(controller);
    if controller.finish().is_ok() {
        0
    } else {
        -1
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
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
            fs::create_dir_all(root.join("private/var/run")).unwrap();
            fs::create_dir_all(root.join("private/var/tmp/launchd")).unwrap();
            fs::write(
                root.join(MARKER_NAME.escape_ascii().to_string()),
                MARKER_VALUE,
            )
            .unwrap();
            fs::set_permissions(
                root.join(MARKER_NAME.escape_ascii().to_string()),
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
        unsafe { response.assume_init() }
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
        assert!(fixture
            .root
            .join("private/var/run/shellspawn.sock")
            .exists());
        let retire = request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce);
        assert_eq!(retire.status, 0);
        assert!(!fixture
            .root
            .join("private/var/run/shellspawn.sock")
            .exists());
        controller.finish().unwrap();
        assert!(!fixture.root.join(".init.pid").exists());
        assert!(!fixture.root.join(".darlingserver.sock").exists());
    }

    #[test]
    fn wrong_nonce_and_unknown_endpoint_fail_closed() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let wrong = request(&controller, CohortEndpoint::Shellspawn, 1, [0x55; 32]);
        assert_ne!(wrong.status, 0);
        assert!(!fixture
            .root
            .join("private/var/run/shellspawn.sock")
            .exists());
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
            CohortEndpoint::Shellspawn,
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
                    .get(&CohortEndpoint::Shellspawn)
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
            .contains_key(&CohortEndpoint::Shellspawn));
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
    fn retained_marker_rejects_in_place_mutation() {
        let fixture = Fixture::new();
        let (controller, _darlingserver) =
            CohortController::start_for_test(&fixture.root, 4242).unwrap();
        let marker = fixture.root.join(".darling-runtime-mode-v1");
        let before = fs::metadata(&marker).unwrap();
        fs::write(&marker, b"privileged-eunion\n").unwrap();
        let after = fs::metadata(&marker).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        let rejected = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
        assert_ne!(rejected.status, 0);
        assert!(!fixture
            .root
            .join("private/var/run/shellspawn.sock")
            .exists());
        fs::write(&marker, MARKER_VALUE).unwrap();
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
            assert!(fixture
                .root
                .join("private/var/run/.darling-lifecycle-controller-v1.sock")
                .exists());
        }
        assert!(!fixture.root.join(".init.pid").exists());
        assert!(!fixture.root.join(".darlingserver.sock").exists());
        assert!(!fixture
            .root
            .join("private/var/run/.darling-lifecycle-controller-v1.sock")
            .exists());
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
        assert!(!fixture
            .root
            .join("private/var/run/shellspawn.sock")
            .exists());
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
        let endpoint = fixture.root.join("private/var/run/shellspawn.sock");
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
        fs::create_dir_all(fixture.root.join("private/var/run")).unwrap();
        fs::create_dir_all(fixture.root.join("private/var/tmp/launchd")).unwrap();
        fs::write(fixture.root.join(".darling-runtime-mode-v1"), MARKER_VALUE).unwrap();
        fs::set_permissions(
            fixture.root.join(".darling-runtime-mode-v1"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();

        assert!(!fixture
            .root
            .join("private/var/run/.darling-lifecycle-controller-v1.sock")
            .exists());
        assert!(!retained.join("private/var/run/shellspawn.sock").exists());
        assert!(!fixture
            .root
            .join("private/var/run/shellspawn.sock")
            .exists());
        fs::remove_dir_all(&fixture.root).unwrap();
        fs::rename(retained, &fixture.root).unwrap();
        assert_eq!(
            request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce).status,
            0
        );
        controller.finish().unwrap();
    }
}
