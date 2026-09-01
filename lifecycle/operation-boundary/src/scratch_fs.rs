//! Safe, collection-specific filesystem operations backed by `rustix`.

#![deny(unsafe_code)]
#![deny(unsafe_op_in_unsafe_fn)]

use rustix::fs::{self, AtFlags, Dir, FlockOperation, Mode, OFlags, RenameFlags, Stat};
use std::ffi::{CStr, CString};
use std::io;
use std::os::fd::{BorrowedFd, OwnedFd};
use std::time::Instant;

fn io_error(error: rustix::io::Errno) -> io::Error {
    io::Error::from_raw_os_error(error.raw_os_error())
}

pub(crate) fn fstat(fd: BorrowedFd<'_>) -> io::Result<Stat> {
    fs::fstat(fd).map_err(io_error)
}

pub(crate) fn named_stat(parent: BorrowedFd<'_>, name: &CStr) -> io::Result<Stat> {
    fs::statat(parent, name, AtFlags::SYMLINK_NOFOLLOW).map_err(io_error)
}

pub(crate) fn open_directory(parent: BorrowedFd<'_>, name: &CStr) -> io::Result<OwnedFd> {
    fs::openat(
        parent,
        name,
        OFlags::RDONLY | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(io_error)
}

pub(crate) fn open_path(parent: BorrowedFd<'_>, name: &CStr) -> io::Result<OwnedFd> {
    fs::openat(
        parent,
        name,
        OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        Mode::empty(),
    )
    .map_err(io_error)
}

pub(crate) fn open_rw(parent: BorrowedFd<'_>, name: &CStr) -> io::Result<OwnedFd> {
    fs::openat(
        parent,
        name,
        OFlags::RDWR | OFlags::NOFOLLOW | OFlags::CLOEXEC | OFlags::NONBLOCK,
        Mode::empty(),
    )
    .map_err(io_error)
}

pub(crate) fn create_exclusive(
    parent: BorrowedFd<'_>,
    name: &CStr,
    mode: Mode,
) -> io::Result<OwnedFd> {
    fs::openat(
        parent,
        name,
        OFlags::RDWR | OFlags::CREATE | OFlags::EXCL | OFlags::NOFOLLOW | OFlags::CLOEXEC,
        mode,
    )
    .map_err(io_error)
}

pub(crate) fn chmod(fd: BorrowedFd<'_>, mode: Mode) -> io::Result<()> {
    fs::fchmod(fd, mode).map_err(io_error)
}

pub(crate) fn sync(fd: BorrowedFd<'_>) -> io::Result<()> {
    fs::fsync(fd).map_err(io_error)
}

pub(crate) fn unlink(parent: BorrowedFd<'_>, name: &CStr) -> io::Result<()> {
    fs::unlinkat(parent, name, AtFlags::empty()).map_err(io_error)
}

pub(crate) fn remove_directory(parent: BorrowedFd<'_>, name: &CStr) -> io::Result<()> {
    fs::unlinkat(parent, name, AtFlags::REMOVEDIR).map_err(io_error)
}

pub(crate) fn rename_noreplace(
    parent: BorrowedFd<'_>,
    source: &CStr,
    destination: &CStr,
) -> io::Result<()> {
    fs::renameat_with(parent, source, parent, destination, RenameFlags::NOREPLACE).map_err(io_error)
}

pub(crate) fn lock_exclusive_nonblocking(fd: BorrowedFd<'_>) -> io::Result<()> {
    fs::flock(fd, FlockOperation::NonBlockingLockExclusive).map_err(io_error)
}

pub(crate) fn unlock(fd: BorrowedFd<'_>) -> io::Result<()> {
    fs::flock(fd, FlockOperation::Unlock).map_err(io_error)
}

pub(crate) fn entries(
    directory: BorrowedFd<'_>,
    limit: usize,
    deadline: Instant,
) -> io::Result<Vec<CString>> {
    if limit == 0 {
        return Err(io::Error::from_raw_os_error(libc::EFBIG));
    }
    let mut stream = Dir::read_from(directory).map_err(io_error)?;
    let mut result = Vec::new();
    for entry in &mut stream {
        if Instant::now() >= deadline {
            return Err(io::Error::from_raw_os_error(libc::ETIMEDOUT));
        }
        let entry = entry.map_err(io_error)?;
        let name = entry.file_name();
        if name.to_bytes() == b"." || name.to_bytes() == b".." {
            continue;
        }
        if result.len() >= limit {
            return Err(io::Error::from_raw_os_error(libc::EFBIG));
        }
        result.push(name.to_owned());
    }
    Ok(result)
}

pub(crate) fn parent_pid() -> Option<u32> {
    rustix::process::getppid().and_then(|pid| u32::try_from(pid.as_raw_nonzero().get()).ok())
}

#[cfg(test)]
pub(crate) mod test_support {
    use super::*;

    pub(crate) fn lock_for_owner(fd: BorrowedFd<'_>) -> io::Result<()> {
        lock_exclusive_nonblocking(fd)
    }

    pub(crate) fn unlock_for_recovery(fd: BorrowedFd<'_>) -> io::Result<()> {
        unlock(fd)
    }
}
