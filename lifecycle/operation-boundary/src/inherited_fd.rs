//! Minimal transport boundary for duplicating an inherited raw descriptor.

use std::io;
use std::os::fd::{FromRawFd, OwnedFd, RawFd};

pub(crate) fn duplicate_cloexec(raw_fd: RawFd, minimum: RawFd) -> io::Result<OwnedFd> {
    // SAFETY: `fcntl` treats an invalid raw descriptor as the ordinary EBADF
    // kernel error. A successful F_DUPFD_CLOEXEC return is a fresh descriptor
    // not owned anywhere else; ownership is transferred to OwnedFd exactly
    // once in the same block.
    unsafe {
        let duplicate = libc::fcntl(raw_fd, libc::F_DUPFD_CLOEXEC, minimum);
        if duplicate < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(OwnedFd::from_raw_fd(duplicate))
    }
}
