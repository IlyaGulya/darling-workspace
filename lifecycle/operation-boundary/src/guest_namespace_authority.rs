//! FD-relative pilot for guest namespace authority.
//!
//! This module deliberately covers only create/mkdir, unlink/whiteout and one
//! rename.  It never accepts an absolute mutation path after acquisition.

use libc::{self, c_int};
use std::ffi::{CStr, CString};
use std::fmt;
use std::io;
use std::mem::{size_of, zeroed};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::path::Path;
use std::ptr;
use std::sync::atomic::{AtomicU32, Ordering};
use std::time::{Duration, Instant};

const MAGIC: [u8; 8] = *b"DLGNSA2\0";
const VERSION: u32 = 2;
const ACTIVE: u32 = 1;
const REVOKED: u32 = 2;
const MAX_COMPONENTS: usize = 128;

#[derive(Debug)]
pub enum AuthorityError {
    Io(&'static str, io::Error),
    InvalidPath,
    IdentityMismatch,
    Revoked,
    DrainPending,
    Protocol(&'static str),
}

impl fmt::Display for AuthorityError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Io(op, error) => write!(f, "{op}: {error}"),
            Self::InvalidPath => write!(f, "invalid relative guest path"),
            Self::IdentityMismatch => write!(f, "retained identity mismatch"),
            Self::Revoked => write!(f, "guest namespace authority revoked"),
            Self::DrainPending => write!(f, "guest namespace mutation drain pending"),
            Self::Protocol(message) => write!(f, "bootstrap protocol: {message}"),
        }
    }
}

impl std::error::Error for AuthorityError {}

type Result<T> = std::result::Result<T, AuthorityError>;

fn io(op: &'static str) -> AuthorityError {
    AuthorityError::Io(op, io::Error::last_os_error())
}

#[repr(C)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
}

fn identity(fd: RawFd) -> Result<FileIdentity> {
    let mut stat: libc::stat = unsafe { zeroed() };
    if unsafe { libc::fstat(fd, &mut stat) } != 0 {
        return Err(io("fstat"));
    }
    Ok(FileIdentity {
        device: stat.st_dev,
        inode: stat.st_ino,
    })
}

fn identity_at(parent: RawFd, name: &CStr) -> Result<FileIdentity> {
    let mut stat: libc::stat = unsafe { zeroed() };
    if unsafe { libc::fstatat(parent, name.as_ptr(), &mut stat, libc::AT_SYMLINK_NOFOLLOW) } != 0 {
        return Err(io("fstatat named capability"));
    }
    Ok(FileIdentity {
        device: stat.st_dev,
        inode: stat.st_ino,
    })
}

#[repr(C)]
struct LeasePage {
    magic: [u8; 8],
    version: u32,
    state: AtomicU32,
    generation: u64,
    prefix: FileIdentity,
    lock: FileIdentity,
    gate: FileIdentity,
    controller_pid: i32,
}

#[repr(C)]
#[derive(Clone, Copy)]
pub struct BootstrapEnvelope {
    pub magic: [u8; 8],
    pub version: u32,
    pub required: u32,
    pub generation: u64,
    pub prefix: FileIdentity,
    pub lock: FileIdentity,
    pub gate: FileIdentity,
    pub descriptor_count: u32,
    pub reserved: u32,
}

pub struct GuestNamespaceAuthority {
    prefix: OwnedFd,
    lock: OwnedFd,
    gate: OwnedFd,
    lease: OwnedFd,
    controller_pidfd: OwnedFd,
    page: *mut LeasePage,
    envelope: BootstrapEnvelope,
}

unsafe impl Send for GuestNamespaceAuthority {}
// The mapped lease page exposes only an AtomicU32 state to shared readers.
// All descriptors and envelope fields are immutable after construction;
// revocation is sequenced after the external gate drain.
unsafe impl Sync for GuestNamespaceAuthority {}

impl GuestNamespaceAuthority {
    pub(crate) fn prefix_fd(&self) -> RawFd {
        self.prefix.as_raw_fd()
    }

    pub(crate) fn generation(&self) -> u64 {
        self.envelope.generation
    }

    pub(crate) fn is_active(&self) -> bool {
        unsafe { (*self.page).state.load(Ordering::Acquire) == ACTIVE }
    }

    pub fn issue(prefix_path: &Path, generation: u64) -> Result<Self> {
        if generation == 0 {
            return Err(AuthorityError::Protocol("zero generation"));
        }
        let prefix_path = CString::new(prefix_path.as_os_str().as_encoded_bytes())
            .map_err(|_| AuthorityError::InvalidPath)?;
        let prefix = owned(
            unsafe {
                libc::open(
                    prefix_path.as_ptr(),
                    libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            },
            "open prefix",
        )?;
        Self::issue_from_owned(prefix, generation)
    }

    pub fn issue_from_fd(prefix_fd: RawFd, generation: u64) -> Result<Self> {
        if generation == 0 {
            return Err(AuthorityError::Protocol("zero generation"));
        }
        let prefix = duplicate(prefix_fd, "duplicate retained prefix")?;
        Self::issue_from_owned(prefix, generation)
    }

    pub fn issue_from_locked_fds(
        prefix_fd: RawFd,
        lock_fd: RawFd,
        generation: u64,
    ) -> Result<Self> {
        if generation == 0 {
            return Err(AuthorityError::Protocol("zero generation"));
        }
        let prefix = duplicate(prefix_fd, "duplicate retained prefix")?;
        let lock = duplicate(lock_fd, "duplicate retained lifecycle lease")?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(io("validate exclusive lifecycle flock"));
        }
        Self::issue_with_lock(prefix, lock, generation)
    }

    fn issue_from_owned(prefix: OwnedFd, generation: u64) -> Result<Self> {
        let lock_name = cstr(b".lifecycle.lock\0");
        let lock = owned(
            unsafe {
                libc::openat(
                    prefix.as_raw_fd(),
                    lock_name.as_ptr(),
                    libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            },
            "open lifecycle lock",
        )?;
        if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
            return Err(io("exclusive lifecycle flock"));
        }
        Self::issue_with_lock(prefix, lock, generation)
    }

    fn issue_with_lock(prefix: OwnedFd, lock: OwnedFd, generation: u64) -> Result<Self> {
        let gate = memfd("darling-guest-namespace-gate")?;
        let controller_pidfd = owned(
            unsafe { libc::syscall(libc::SYS_pidfd_open, libc::getpid(), 0) as c_int },
            "pidfd_open controller",
        )?;
        let lease = memfd("darling-guest-namespace-lease")?;
        if unsafe { libc::ftruncate(lease.as_raw_fd(), size_of::<LeasePage>() as libc::off_t) } != 0
        {
            return Err(io("size lease"));
        }
        let page = unsafe {
            libc::mmap(
                ptr::null_mut(),
                size_of::<LeasePage>(),
                libc::PROT_READ | libc::PROT_WRITE,
                libc::MAP_SHARED,
                lease.as_raw_fd(),
                0,
            )
        } as *mut LeasePage;
        if page.cast::<libc::c_void>() == libc::MAP_FAILED {
            return Err(io("map lease"));
        }
        let prefix_identity = identity(prefix.as_raw_fd())?;
        let lock_identity = identity(lock.as_raw_fd())?;
        let gate_identity = identity(gate.as_raw_fd())?;
        unsafe {
            ptr::write(
                page,
                LeasePage {
                    magic: MAGIC,
                    version: VERSION,
                    state: AtomicU32::new(ACTIVE),
                    generation,
                    prefix: prefix_identity,
                    lock: lock_identity,
                    gate: gate_identity,
                    controller_pid: libc::getpid(),
                },
            );
        }
        let envelope = BootstrapEnvelope {
            magic: MAGIC,
            version: VERSION,
            required: 1,
            generation,
            prefix: prefix_identity,
            lock: lock_identity,
            gate: gate_identity,
            descriptor_count: 5,
            reserved: 0,
        };
        Ok(Self {
            prefix,
            lock,
            gate,
            lease,
            controller_pidfd,
            page,
            envelope,
        })
    }

    pub fn send_bootstrap(&self, socket: RawFd) -> Result<()> {
        let descriptors = [
            self.lease.as_raw_fd(),
            self.gate.as_raw_fd(),
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.controller_pidfd.as_raw_fd(),
        ];
        send_fds(socket, &self.envelope, &descriptors)
    }

    /// Send the authority envelope plus the exact retained runtime lower root.
    /// The sixth descriptor is consumed only by mldr; the five authority
    /// descriptors retain their stable ordering for libsystem_kernel.
    pub fn send_bootstrap_with_directory(&self, socket: RawFd, directory: RawFd) -> Result<()> {
        let mut envelope = self.envelope;
        envelope.descriptor_count = 6;
        let descriptors = [
            self.lease.as_raw_fd(),
            self.gate.as_raw_fd(),
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.controller_pidfd.as_raw_fd(),
            directory,
        ];
        send_fds(socket, &envelope, &descriptors)
    }

    pub fn revoke(&mut self) -> Result<()> {
        self.revoke_with_deadline(Duration::from_millis(100))
    }

    fn revoke_with_deadline(&mut self, budget: Duration) -> Result<()> {
        unsafe { (*self.page).state.store(REVOKED, Ordering::Release) };
        let deadline = Instant::now() + budget;
        loop {
            if unsafe { libc::flock(self.gate.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
                return Ok(());
            }
            let error = io::Error::last_os_error();
            if error.raw_os_error() != Some(libc::EWOULDBLOCK) {
                return Err(AuthorityError::Io("drain mutation gate", error));
            }
            if Instant::now() >= deadline {
                return Err(AuthorityError::DrainPending);
            }
            std::thread::yield_now();
        }
    }
}

impl Drop for GuestNamespaceAuthority {
    fn drop(&mut self) {
        // Closing a controller must close admission before any owning capability
        // can disappear. A concurrent operation may delay the bounded drain, but
        // the shared state is already irreversibly REVOKED.
        let _ = self.revoke_with_deadline(Duration::from_millis(10));
        unsafe {
            libc::munmap(self.page.cast(), size_of::<LeasePage>());
        }
    }
}

pub struct SessionCapabilities {
    lease: OwnedFd,
    gate: OwnedFd,
    prefix: OwnedFd,
    lock: OwnedFd,
    controller_pidfd: OwnedFd,
    page: *mut LeasePage,
    envelope: BootstrapEnvelope,
}

impl SessionCapabilities {
    pub fn receive_required(socket: RawFd) -> Result<Self> {
        let (envelope, descriptors) = receive_fds(socket)?;
        if envelope.magic != MAGIC
            || envelope.version != VERSION
            || envelope.required != 1
            || envelope.descriptor_count != 5
            || envelope.reserved != 0
            || envelope.generation == 0
            || descriptors.len() != 5
        {
            return Err(AuthorityError::Protocol("malformed required bootstrap"));
        }
        let mut descriptors = descriptors.into_iter();
        let lease = descriptors.next().unwrap();
        let gate = descriptors.next().unwrap();
        let prefix = descriptors.next().unwrap();
        let lock = descriptors.next().unwrap();
        let controller_pidfd = descriptors.next().unwrap();
        if identity(prefix.as_raw_fd())? != envelope.prefix
            || identity(lock.as_raw_fd())? != envelope.lock
            || identity(gate.as_raw_fd())? != envelope.gate
        {
            return Err(AuthorityError::IdentityMismatch);
        }
        let page = unsafe {
            libc::mmap(
                ptr::null_mut(),
                size_of::<LeasePage>(),
                libc::PROT_READ,
                libc::MAP_SHARED,
                lease.as_raw_fd(),
                0,
            )
        } as *mut LeasePage;
        if page.cast::<libc::c_void>() == libc::MAP_FAILED {
            return Err(io("map received lease"));
        }
        let valid = unsafe {
            (*page).magic == MAGIC
                && (*page).version == VERSION
                && (*page).generation == envelope.generation
                && (*page).prefix == envelope.prefix
                && (*page).lock == envelope.lock
                && (*page).gate == envelope.gate
                && (*page).state.load(Ordering::Acquire) == ACTIVE
        };
        if !valid {
            unsafe {
                libc::munmap(page.cast(), size_of::<LeasePage>());
            }
            return Err(AuthorityError::Protocol("lease/envelope mismatch"));
        }
        Ok(Self {
            lease,
            gate,
            prefix,
            lock,
            controller_pidfd,
            page,
            envelope,
        })
    }

    pub fn authorize(&self) -> Result<MutationGuard<'_>> {
        let mut pollfd = libc::pollfd {
            fd: self.controller_pidfd.as_raw_fd(),
            events: libc::POLLIN | libc::POLLHUP | libc::POLLERR,
            revents: 0,
        };
        if unsafe { libc::poll(&mut pollfd, 1, 0) } != 0 {
            return Err(AuthorityError::Revoked);
        }
        if unsafe { (*self.page).state.load(Ordering::Acquire) } != ACTIVE {
            return Err(AuthorityError::Revoked);
        }
        let gate_path = CString::new(format!("/proc/self/fd/{}", self.gate.as_raw_fd())).unwrap();
        let gate = owned(
            unsafe { libc::open(gate_path.as_ptr(), libc::O_RDWR | libc::O_CLOEXEC) },
            "reopen independent operation gate",
        )?;
        if unsafe { libc::flock(gate.as_raw_fd(), libc::LOCK_SH | libc::LOCK_NB) } != 0 {
            return Err(AuthorityError::Revoked);
        }
        let validation: Result<bool> = (|| {
            Ok(identity(self.prefix.as_raw_fd())? == self.envelope.prefix
                && identity(self.lock.as_raw_fd())? == self.envelope.lock
                && identity_at(self.prefix.as_raw_fd(), cstr(b".lifecycle.lock\0"))?
                    == self.envelope.lock
                && identity(self.gate.as_raw_fd())? == self.envelope.gate
                && unsafe { (*self.page).state.load(Ordering::Acquire) } == ACTIVE)
        })();
        if !matches!(validation, Ok(true)) {
            return Err(validation.err().unwrap_or(AuthorityError::IdentityMismatch));
        }
        Ok(MutationGuard {
            session: self,
            _gate: gate,
        })
    }

    pub fn protected_fds(&self) -> [RawFd; 5] {
        [
            self.lease.as_raw_fd(),
            self.gate.as_raw_fd(),
            self.prefix.as_raw_fd(),
            self.lock.as_raw_fd(),
            self.controller_pidfd.as_raw_fd(),
        ]
    }
}

impl Drop for SessionCapabilities {
    fn drop(&mut self) {
        unsafe {
            libc::munmap(self.page.cast(), size_of::<LeasePage>());
        }
    }
}

pub struct MutationGuard<'a> {
    session: &'a SessionCapabilities,
    _gate: OwnedFd,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ResolvedLayer {
    Upper,
}

pub struct ResolvedParent {
    parent: OwnedFd,
    leaf: CString,
    pub parent_identity: FileIdentity,
    pub layer: ResolvedLayer,
}

impl MutationGuard<'_> {
    fn revalidate(&self) -> Result<()> {
        if unsafe { (*self.session.page).state.load(Ordering::Acquire) } != ACTIVE {
            return Err(AuthorityError::Revoked);
        }
        if identity(self.session.prefix.as_raw_fd())? != self.session.envelope.prefix
            || identity_at(self.session.prefix.as_raw_fd(), cstr(b".lifecycle.lock\0"))?
                != self.session.envelope.lock
        {
            return Err(AuthorityError::IdentityMismatch);
        }
        Ok(())
    }

    pub fn resolve_parent(&self, guest_relative: &str) -> Result<ResolvedParent> {
        let mut parts = guest_relative.split('/').peekable();
        let mut parent = duplicate(self.session.prefix.as_raw_fd(), "duplicate prefix")?;
        let mut count = 0;
        let leaf = loop {
            let part = parts.next().ok_or(AuthorityError::InvalidPath)?;
            count += 1;
            if count > MAX_COMPONENTS
                || part.is_empty()
                || part == "."
                || part == ".."
                || part.as_bytes().contains(&0)
            {
                return Err(AuthorityError::InvalidPath);
            }
            if parts.peek().is_none() {
                break CString::new(part).unwrap();
            }
            let component = CString::new(part).unwrap();
            parent = owned(
                unsafe {
                    libc::openat(
                        parent.as_raw_fd(),
                        component.as_ptr(),
                        libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                    )
                },
                "resolve retained parent",
            )?;
        };
        let parent_identity = identity(parent.as_raw_fd())?;
        Ok(ResolvedParent {
            parent,
            leaf,
            parent_identity,
            layer: ResolvedLayer::Upper,
        })
    }

    pub fn mkdir(&self, resolved: &ResolvedParent, mode: libc::mode_t) -> Result<()> {
        self.revalidate()?;
        if unsafe { libc::mkdirat(resolved.parent.as_raw_fd(), resolved.leaf.as_ptr(), mode) } != 0
        {
            return Err(io("mkdirat retained parent"));
        }
        Ok(())
    }

    pub fn create(&self, resolved: &ResolvedParent, mode: libc::mode_t) -> Result<OwnedFd> {
        self.revalidate()?;
        owned(
            unsafe {
                libc::openat(
                    resolved.parent.as_raw_fd(),
                    resolved.leaf.as_ptr(),
                    libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_CLOEXEC
                        | libc::O_NOFOLLOW,
                    mode,
                )
            },
            "openat create retained parent",
        )
    }

    pub fn unlink(&self, resolved: &ResolvedParent) -> Result<()> {
        self.revalidate()?;
        if unsafe { libc::unlinkat(resolved.parent.as_raw_fd(), resolved.leaf.as_ptr(), 0) } != 0 {
            return Err(io("unlinkat retained parent"));
        }
        Ok(())
    }

    pub fn rename(&self, source: &ResolvedParent, destination: &ResolvedParent) -> Result<()> {
        self.revalidate()?;
        if unsafe {
            libc::renameat(
                source.parent.as_raw_fd(),
                source.leaf.as_ptr(),
                destination.parent.as_raw_fd(),
                destination.leaf.as_ptr(),
            )
        } != 0
        {
            return Err(io("renameat retained parents"));
        }
        Ok(())
    }
}

fn cstr(bytes: &'static [u8]) -> &'static CStr {
    CStr::from_bytes_with_nul(bytes).unwrap()
}

fn owned(fd: c_int, operation: &'static str) -> Result<OwnedFd> {
    if fd < 0 {
        Err(io(operation))
    } else {
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }
}

fn duplicate(fd: RawFd, operation: &'static str) -> Result<OwnedFd> {
    owned(
        unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 3) },
        operation,
    )
}

fn memfd(name: &str) -> Result<OwnedFd> {
    let name = CString::new(name).unwrap();
    owned(
        unsafe { libc::syscall(libc::SYS_memfd_create, name.as_ptr(), libc::MFD_CLOEXEC) as c_int },
        "memfd_create",
    )
}

fn send_fds(socket: RawFd, envelope: &BootstrapEnvelope, fds: &[RawFd]) -> Result<()> {
    let mut iov = libc::iovec {
        iov_base: (envelope as *const BootstrapEnvelope).cast_mut().cast(),
        iov_len: size_of::<BootstrapEnvelope>(),
    };
    let space = unsafe { libc::CMSG_SPACE(std::mem::size_of_val(fds) as u32) as usize };
    let mut control = vec![0u8; space];
    let mut message: libc::msghdr = unsafe { zeroed() };
    message.msg_iov = &mut iov;
    message.msg_iovlen = 1;
    message.msg_control = control.as_mut_ptr().cast();
    message.msg_controllen = control.len();
    unsafe {
        let header = libc::CMSG_FIRSTHDR(&message);
        (*header).cmsg_level = libc::SOL_SOCKET;
        (*header).cmsg_type = libc::SCM_RIGHTS;
        (*header).cmsg_len = libc::CMSG_LEN(std::mem::size_of_val(fds) as u32) as usize;
        ptr::copy_nonoverlapping(
            fds.as_ptr().cast::<u8>(),
            libc::CMSG_DATA(header),
            std::mem::size_of_val(fds),
        );
    }
    let sent = unsafe { libc::sendmsg(socket, &message, libc::MSG_NOSIGNAL) };
    if sent != size_of::<BootstrapEnvelope>() as isize {
        return Err(io("send bootstrap"));
    }
    Ok(())
}

fn receive_fds(socket: RawFd) -> Result<(BootstrapEnvelope, Vec<OwnedFd>)> {
    let mut envelope: BootstrapEnvelope = unsafe { zeroed() };
    let mut iov = libc::iovec {
        iov_base: (&mut envelope as *mut BootstrapEnvelope).cast(),
        iov_len: size_of::<BootstrapEnvelope>(),
    };
    let space = unsafe { libc::CMSG_SPACE((5 * size_of::<RawFd>()) as u32) as usize };
    let mut control = vec![0u8; space];
    let mut message: libc::msghdr = unsafe { zeroed() };
    message.msg_iov = &mut iov;
    message.msg_iovlen = 1;
    message.msg_control = control.as_mut_ptr().cast();
    message.msg_controllen = control.len();
    let received = unsafe { libc::recvmsg(socket, &mut message, libc::MSG_CMSG_CLOEXEC) };
    // Collect every descriptor installed by the kernel before validating the
    // envelope. OwnedFd then closes malformed/truncated ancillary data too.
    let header = unsafe { libc::CMSG_FIRSTHDR(&message) };
    let mut received_fds = Vec::new();
    let mut cursor = header;
    while !cursor.is_null() {
        if unsafe {
            (*cursor).cmsg_level == libc::SOL_SOCKET && (*cursor).cmsg_type == libc::SCM_RIGHTS
        } {
            let bytes =
                unsafe { (*cursor).cmsg_len }.saturating_sub(unsafe { libc::CMSG_LEN(0) as usize });
            let count = bytes / size_of::<RawFd>();
            for index in 0..count {
                let fd = unsafe { *libc::CMSG_DATA(cursor).cast::<RawFd>().add(index) };
                received_fds.push(unsafe { OwnedFd::from_raw_fd(fd) });
            }
        }
        cursor = unsafe { libc::CMSG_NXTHDR(&message, cursor) };
    }
    if received != size_of::<BootstrapEnvelope>() as isize
        || message.msg_flags & (libc::MSG_TRUNC | libc::MSG_CTRUNC) != 0
    {
        return Err(AuthorityError::Protocol("truncated bootstrap"));
    }
    if header.is_null()
        || unsafe {
            (*header).cmsg_level != libc::SOL_SOCKET || (*header).cmsg_type != libc::SCM_RIGHTS
        }
        || !unsafe { libc::CMSG_NXTHDR(&message, header) }.is_null()
    {
        return Err(AuthorityError::Protocol("exact SCM_RIGHTS required"));
    }
    let bytes = unsafe { (*header).cmsg_len } - unsafe { libc::CMSG_LEN(0) as usize };
    if bytes != 5 * size_of::<RawFd>() || received_fds.len() != 5 {
        return Err(AuthorityError::Protocol("exact descriptor set required"));
    }
    if envelope.magic != MAGIC
        || envelope.version != VERSION
        || envelope.required != 1
        || envelope.descriptor_count != 5
        || envelope.reserved != 0
        || envelope.generation == 0
    {
        #[cfg(test)]
        let raw: Vec<_> = received_fds.iter().map(AsRawFd::as_raw_fd).collect();
        drop(received_fds);
        #[cfg(test)]
        for fd in raw {
            assert_eq!(unsafe { libc::fcntl(fd, libc::F_GETFD) }, -1);
        }
        return Err(AuthorityError::Protocol("malformed bootstrap envelope"));
    }
    Ok((envelope, received_fds))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{symlink, MetadataExt};
    use std::process::Command;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture() -> (
        std::path::PathBuf,
        GuestNamespaceAuthority,
        SessionCapabilities,
    ) {
        let root = std::env::temp_dir().join(format!(
            "darling-fd-authority-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        fs::create_dir(&root).unwrap();
        fs::write(root.join(".lifecycle.lock"), b"").unwrap();
        let authority = GuestNamespaceAuthority::issue(&root, 7).unwrap();
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
        authority.send_bootstrap(sockets[0]).unwrap();
        let session = SessionCapabilities::receive_required(sockets[1]).unwrap();
        unsafe {
            libc::close(sockets[0]);
            libc::close(sockets[1]);
        }
        (root, authority, session)
    }

    #[test]
    fn pilot_is_fd_relative_after_prefix_swap() {
        let (root, _authority, session) = fixture();
        fs::create_dir(root.join("parent")).unwrap();
        let guard = session.authorize().unwrap();
        let resolved = guard.resolve_parent("parent/created").unwrap();
        let retained_parent = fs::metadata(root.join("parent")).unwrap().ino();
        let saved = root.with_extension("retained");
        fs::rename(&root, &saved).unwrap();
        fs::create_dir(&root).unwrap();
        fs::create_dir(root.join("parent")).unwrap();
        guard.create(&resolved, 0o600).unwrap();
        assert!(saved.join("parent/created").exists());
        assert!(!root.join("parent/created").exists());
        assert_eq!(
            fs::metadata(saved.join("parent")).unwrap().ino(),
            retained_parent
        );
        fs::remove_dir_all(&root).unwrap();
        fs::remove_dir_all(&saved).unwrap();
    }

    #[test]
    fn retained_parent_survives_replacement_for_unlink_and_rename() {
        let (root, _authority, session) = fixture();
        fs::create_dir(root.join("parent")).unwrap();
        fs::write(root.join("parent/source"), b"original").unwrap();
        let guard = session.authorize().unwrap();
        let source = guard.resolve_parent("parent/source").unwrap();
        let destination = guard.resolve_parent("parent/destination").unwrap();
        let old_parent = root.join("parent.old");
        fs::rename(root.join("parent"), &old_parent).unwrap();
        fs::create_dir(root.join("parent")).unwrap();
        fs::write(root.join("parent/source"), b"replacement").unwrap();
        guard.rename(&source, &destination).unwrap();
        assert_eq!(
            fs::read(old_parent.join("destination")).unwrap(),
            b"original"
        );
        assert_eq!(
            fs::read(root.join("parent/source")).unwrap(),
            b"replacement"
        );
        guard.unlink(&destination).unwrap();
        assert_eq!(
            fs::read(root.join("parent/source")).unwrap(),
            b"replacement"
        );
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn symlink_parent_is_rejected_and_revocation_drains() {
        let (root, mut authority, session) = fixture();
        fs::create_dir(root.join("real")).unwrap();
        symlink("real", root.join("alias")).unwrap();
        let guard = session.authorize().unwrap();
        assert!(guard.resolve_parent("alias/file").is_err());
        drop(guard);
        authority.revoke().unwrap();
        assert!(matches!(session.authorize(), Err(AuthorityError::Revoked)));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn named_lock_replacement_is_rejected_before_resolve() {
        let (root, _authority, session) = fixture();
        fs::rename(
            root.join(".lifecycle.lock"),
            root.join(".lifecycle.lock.old"),
        )
        .unwrap();
        fs::write(root.join(".lifecycle.lock"), b"replacement").unwrap();
        assert!(matches!(
            session.authorize(),
            Err(AuthorityError::IdentityMismatch)
        ));
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn lock_replacement_after_authorization_is_rejected_before_mutation() {
        let (root, _authority, session) = fixture();
        fs::create_dir(root.join("parent")).unwrap();
        let guard = session.authorize().unwrap();
        let target = guard.resolve_parent("parent/rejected").unwrap();
        fs::rename(
            root.join(".lifecycle.lock"),
            root.join(".lifecycle.lock.old"),
        )
        .unwrap();
        fs::write(root.join(".lifecycle.lock"), b"replacement").unwrap();
        assert!(matches!(
            guard.create(&target, 0o600),
            Err(AuthorityError::IdentityMismatch)
        ));
        assert!(!root.join("parent/rejected").exists());
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn revocation_closes_admission_and_bounded_drain_can_retry() {
        let (root, mut authority, session) = fixture();
        fs::create_dir(root.join("parent")).unwrap();
        let guard = session.authorize().unwrap();
        assert!(authority.revoke().is_err());
        assert!(matches!(session.authorize(), Err(AuthorityError::Revoked)));
        let target = guard.resolve_parent("parent/inflight").unwrap();
        assert!(matches!(
            guard.create(&target, 0o600),
            Err(AuthorityError::Revoked)
        ));
        assert!(!root.join("parent/inflight").exists());
        drop(guard);
        authority.revoke().unwrap();
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn malformed_or_missing_bootstrap_fails_closed() {
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
        assert_eq!(
            unsafe { libc::send(sockets[0], b"bad".as_ptr().cast(), 3, 0) },
            3
        );
        assert!(SessionCapabilities::receive_required(sockets[1]).is_err());
        unsafe {
            libc::close(sockets[0]);
            libc::close(sockets[1]);
        }

        let mut missing = [0; 2];
        assert_eq!(
            unsafe {
                libc::socketpair(
                    libc::AF_UNIX,
                    libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                    0,
                    missing.as_mut_ptr(),
                )
            },
            0
        );
        unsafe { libc::close(missing[0]) };
        assert!(SessionCapabilities::receive_required(missing[1]).is_err());
        unsafe { libc::close(missing[1]) };
    }

    #[test]
    fn malformed_scm_rights_closes_every_installed_descriptor() {
        const HELPER: &str = "DARLING_GNA_MALFORMED_SCM_HELPER";
        if std::env::var_os(HELPER).is_none() {
            let status = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "guest_namespace_authority::tests::malformed_scm_rights_closes_every_installed_descriptor",
                    "--nocapture",
                ])
                .env(HELPER, "1")
                .status()
                .unwrap();
            assert!(status.success());
            return;
        }
        let (root, authority, _session) = fixture();
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
        let mut forged = authority.envelope;
        forged.reserved = 1;
        let descriptors = [
            authority.lease.as_raw_fd(),
            authority.gate.as_raw_fd(),
            authority.prefix.as_raw_fd(),
            authority.lock.as_raw_fd(),
            authority.controller_pidfd.as_raw_fd(),
        ];
        send_fds(sockets[0], &forged, &descriptors).unwrap();
        assert!(SessionCapabilities::receive_required(sockets[1]).is_err());
        unsafe {
            libc::close(sockets[0]);
            libc::close(sockets[1]);
        }
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn controller_sigkill_revokes_admission_through_pidfd() {
        let root =
            std::env::temp_dir().join(format!("darling-fd-authority-death-{}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir(&root).unwrap();
        fs::write(root.join(".lifecycle.lock"), b"").unwrap();
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
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::close(sockets[1]) };
            let authority = GuestNamespaceAuthority::issue(&root, 19).unwrap();
            authority.send_bootstrap(sockets[0]).unwrap();
            loop {
                unsafe { libc::pause() };
            }
        }
        unsafe { libc::close(sockets[0]) };
        let session = SessionCapabilities::receive_required(sockets[1]).unwrap();
        unsafe { libc::close(sockets[1]) };
        assert!(session.authorize().is_ok());
        assert_eq!(unsafe { libc::kill(child, libc::SIGKILL) }, 0);
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert!(matches!(session.authorize(), Err(AuthorityError::Revoked)));
        drop(session);
        fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn pilot_create_unlink_and_rename_use_retained_parents() {
        let (root, _authority, session) = fixture();
        fs::create_dir(root.join("pilot")).unwrap();
        let guard = session.authorize().unwrap();
        let source = guard.resolve_parent("pilot/source").unwrap();
        let destination = guard.resolve_parent("pilot/destination").unwrap();
        guard.create(&source, 0o600).unwrap();
        guard.rename(&source, &destination).unwrap();
        guard.unlink(&destination).unwrap();
        assert!(!root.join("pilot/destination").exists());
        drop(guard);
        fs::remove_dir_all(&root).unwrap();
    }
}
