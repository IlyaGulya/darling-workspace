//! Concrete Linux backend for the architecture controller.
//!
//! This module is intentionally a fixture-facing backend.  It opens every
//! runtime object relative to an inherited anchor, retains pidfds for the
//! session root and members, and performs endpoint removal through retained
//! directory descriptors.  It is not wired into Darling or any production
//! route; the tests below are the only current caller.

use crate::controller::{
    sealed, AcquisitionBundle, AnchorCapabilities, CapabilityInspector, CleanupFailure,
    ControllerError, EndpointCapability, EndpointCleanupExecutor, EndpointKind,
    LauncherObservation, MarkerObservation, MembershipSnapshot, MembershipSource, PidFdCapability,
    PrefixCapability, ProcessIdentity, QuarantineObligation, QuiescenceBackend, QuiescenceProof,
    SignalEvidence, SignalExecutor, SignalTarget,
};
use crate::FileIdentity;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::ffi::CString;
use std::fs::File;
use std::io::{self, Read};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::thread;
use std::time::{Duration, Instant};

const MARKER_NAME: &[u8] = b".darling-runtime-mode-v1";
const LAUNCHER_NAME: &[u8] = b"darling-launcher";
const INIT_PID_NAME: &[u8] = b".init.pid";
const LOCK_NAME: &[u8] = b".lifecycle.lock";
const SESSION_NAME: &[u8] = b".lifecycle.session";
const QUARANTINE_NAME: &[u8] = b".lifecycle-quarantine";
const RUN_DIR_NAME: &[u8] = b"var/run";
const MARKER_VALUE: &[u8] = b"rootless-eunion\n";
const MAX_FILE_BYTES: usize = 64 * 1024;
const MAX_CHILDREN_BYTES: usize = 16 * 1024;
const MAX_CENSUS_ROUNDS: usize = 8;
const MAX_CENSUS_PROCESSES: usize = crate::controller::MAX_MEMBERS;
const MAX_CGROUP_PROCS: usize = 4096;
const RESUME_WAIT: Duration = Duration::from_millis(100);
const CGROUP_ROOT: &[u8] = b"/sys/fs/cgroup";

const ENDPOINT_NAMES: &[(EndpointKind, &[u8])] = &[
    (EndpointKind::DarlingServer, b"darlingserver.sock"),
    (EndpointKind::Shellspawn, b"shellspawn.sock"),
    (EndpointKind::Launchd, b"launchd.sock"),
];

fn system_error(operation: &'static str) -> ControllerError {
    ControllerError::SystemCall(operation)
}

fn identity(fd: RawFd) -> Result<FileIdentity, ControllerError> {
    FileIdentity::from_fd(fd).map_err(|_| system_error("fstat"))
}

fn component(name: &[u8]) -> Result<CString, ControllerError> {
    if name.is_empty() || name.contains(&0) || name.contains(&b'/') {
        return Err(ControllerError::MalformedRequest);
    }
    CString::new(name).map_err(|_| ControllerError::MalformedRequest)
}

fn openat(parent: RawFd, name: &[u8], flags: libc::c_int) -> Result<OwnedFd, ControllerError> {
    let name = component(name)?;
    // SAFETY: name is a validated NUL-terminated component and the returned
    // descriptor is transferred to the OwnedFd below.
    let fd = unsafe { libc::openat(parent, name.as_ptr(), flags, 0) };
    if fd < 0 {
        return Err(system_error("openat"));
    }
    // SAFETY: fd is freshly returned by openat and uniquely owned here.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn open_relative_path(
    parent: RawFd,
    path: &[u8],
    flags: libc::c_int,
) -> Result<OwnedFd, ControllerError> {
    let mut current = duplicate(parent)?;
    for (index, part) in path.split(|byte| *byte == b'/').enumerate() {
        if part.is_empty() {
            return Err(ControllerError::MalformedRequest);
        }
        let next = openat(current.as_raw_fd(), part, flags | libc::O_DIRECTORY);
        if index + 1 == path.split(|byte| *byte == b'/').count() {
            return next;
        }
        current = next?;
    }
    Err(ControllerError::MalformedRequest)
}

fn open_object(parent: RawFd, name: &[u8]) -> Result<OwnedFd, ControllerError> {
    openat(
        parent,
        name,
        libc::O_PATH | libc::O_CLOEXEC | libc::O_NOFOLLOW,
    )
}

fn open_regular(parent: RawFd, name: &[u8], writable: bool) -> Result<OwnedFd, ControllerError> {
    let access = if writable {
        libc::O_RDWR
    } else {
        libc::O_RDONLY
    };
    openat(parent, name, access | libc::O_CLOEXEC | libc::O_NOFOLLOW)
}

fn duplicate(fd: RawFd) -> Result<OwnedFd, ControllerError> {
    // SAFETY: fcntl duplicates a valid retained descriptor and returns a new
    // descriptor owned by this function.
    let duplicate = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 0) };
    if duplicate < 0 {
        return Err(system_error("fcntl(F_DUPFD_CLOEXEC)"));
    }
    // SAFETY: duplicate is freshly returned by fcntl.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicate) })
}

fn read_fd(fd: RawFd, limit: usize) -> Result<Vec<u8>, ControllerError> {
    // All callers pass regular files.  Resetting the shared OFD offset makes
    // repeated retained-capability validation deterministic after a prior
    // observation.
    if unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } < 0 {
        return Err(system_error("lseek"));
    }
    let mut bytes = Vec::with_capacity(4096);
    let mut buffer = [0u8; 4096];
    loop {
        // SAFETY: buffer is valid for the requested write length.
        let count =
            unsafe { libc::read(fd, buffer.as_mut_ptr().cast::<libc::c_void>(), buffer.len()) };
        if count < 0 {
            return Err(system_error("read"));
        }
        if count == 0 {
            break;
        }
        let count = count as usize;
        if bytes.len().saturating_add(count) > limit {
            return Err(ControllerError::BudgetExceeded);
        }
        bytes.extend_from_slice(&buffer[..count]);
    }
    Ok(bytes)
}

fn digest(bytes: &[u8]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    hasher.finalize().into()
}

fn mode(identity: FileIdentity) -> u32 {
    identity.mode & libc::S_IFMT
}

fn pidfd_open(pid: libc::pid_t) -> Result<OwnedFd, ControllerError> {
    // SAFETY: pid is supplied by a bounded /proc children parser and flags
    // are zero as required by pidfd_open(2).
    let fd = unsafe { libc::syscall(libc::SYS_pidfd_open, pid, 0) as libc::c_int };
    if fd < 0 {
        return Err(ControllerError::MissingPidfd);
    }
    // SAFETY: fd is freshly returned by pidfd_open.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn pidfd_signal(fd: RawFd, target: SignalTarget) -> Result<SignalEvidence, ControllerError> {
    // SAFETY: fd is a retained pidfd; a null siginfo pointer is valid for
    // pidfd_send_signal(2).
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            libc::SIGKILL,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        ) as libc::c_int
    };
    let signal_result = if result == 0 {
        crate::controller::SignalResult::Sent
    } else {
        match io::Error::last_os_error().raw_os_error() {
            Some(libc::ESRCH) => crate::controller::SignalResult::Gone,
            Some(libc::EAGAIN) | Some(libc::ETIMEDOUT) => crate::controller::SignalResult::Deadline,
            Some(libc::EPERM) | Some(libc::EACCES) => crate::controller::SignalResult::Rejected,
            _ => return Err(system_error("pidfd_send_signal")),
        }
    };
    Ok(SignalEvidence::rust_pidfd(target, signal_result))
}

fn pidfd_control_signal(fd: RawFd, signal: libc::c_int) -> Result<(), ControllerError> {
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            signal,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        ) as libc::c_int
    };
    if result == 0 {
        return Ok(());
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Err(ControllerError::MembershipChanged),
        Some(libc::EBADF) => Err(system_error("pidfd_send_signal(control) EBADF")),
        _ => Err(system_error("pidfd_send_signal(control)")),
    }
}

fn pidfd_resume(fd: RawFd) -> Result<ResumeOutcome, ControllerError> {
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            libc::SIGCONT,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        ) as libc::c_int
    };
    if result == 0 {
        return Ok(ResumeOutcome::Resumed);
    }
    match io::Error::last_os_error().raw_os_error() {
        // A process which has already exited is no longer frozen and is a
        // successful unwind for the stop barrier.
        Some(libc::ESRCH) => Ok(ResumeOutcome::Gone),
        Some(libc::EBADF) => Err(system_error("pidfd_send_signal(SIGCONT) EBADF")),
        _ => Err(system_error("pidfd_send_signal(SIGCONT)")),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ResumeOutcome {
    Resumed,
    Gone,
    Killed,
    Failed,
}

fn force_after_resume_failure(fd: RawFd) -> ResumeOutcome {
    match pidfd_force_kill(fd) {
        Ok(outcome) => outcome,
        Err(ControllerError::SystemCall(_))
        | Err(ControllerError::MembershipChanged)
        | Err(ControllerError::IdentityMismatch(_)) => match pidfd_probe(fd) {
            Ok(PidfdProbe::Gone) => ResumeOutcome::Gone,
            Ok(PidfdProbe::Alive) | Err(_) => ResumeOutcome::Failed,
        },
        Err(_) => ResumeOutcome::Failed,
    }
}

fn pidfd_force_kill(fd: RawFd) -> Result<ResumeOutcome, ControllerError> {
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            libc::SIGKILL,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        ) as libc::c_int
    };
    if result == 0 {
        return Ok(ResumeOutcome::Killed);
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Ok(ResumeOutcome::Gone),
        Some(libc::EBADF) => Err(system_error("pidfd_send_signal(SIGKILL) EBADF")),
        _ => Err(system_error("pidfd_send_signal(SIGKILL)")),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum PidfdProbe {
    Alive,
    Gone,
}

fn pidfd_probe(fd: RawFd) -> Result<PidfdProbe, ControllerError> {
    // signal 0 probes a retained pidfd without affecting the process.
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            fd,
            0,
            std::ptr::null::<libc::siginfo_t>(),
            0,
        ) as libc::c_int
    };
    if result == 0 {
        return Ok(PidfdProbe::Alive);
    }
    match io::Error::last_os_error().raw_os_error() {
        Some(libc::ESRCH) => Ok(PidfdProbe::Gone),
        Some(libc::EBADF) => Err(system_error("pidfd_send_signal(0) EBADF")),
        _ => Err(system_error("pidfd_send_signal(0)")),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ProcessStat {
    starttime: u64,
    state: u8,
    parent: libc::pid_t,
    session: libc::pid_t,
}

fn process_stat_at(path: String) -> Result<Option<ProcessStat>, ControllerError> {
    let file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(_) => return Err(system_error("open(/proc/pid/stat)")),
    };
    let mut bytes = Vec::new();
    if let Err(error) = file.take(MAX_FILE_BYTES as u64).read_to_end(&mut bytes) {
        if matches!(error.raw_os_error(), Some(libc::ENOENT) | Some(libc::ESRCH)) {
            return Ok(None);
        }
        return Err(system_error("read(/proc/pid/stat)"));
    }
    let close = bytes
        .iter()
        .rposition(|byte| *byte == b')')
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    let fields = bytes
        .get(close + 2..)
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    let mut fields = fields
        .split(|byte| byte.is_ascii_whitespace())
        .filter(|field| !field.is_empty());
    let state = fields
        .next()
        .and_then(|value| value.first().copied())
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    let parent = fields
        .next()
        .and_then(|value| std::str::from_utf8(value).ok())
        .and_then(|value| value.parse::<libc::pid_t>().ok())
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    // Fields after the state are: ppid, pgrp, session, ... .
    let pgrp = fields
        .next()
        .and_then(|value| std::str::from_utf8(value).ok())
        .and_then(|value| value.parse::<libc::pid_t>().ok())
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    let session = fields
        .next()
        .and_then(|value| std::str::from_utf8(value).ok())
        .and_then(|value| value.parse::<libc::pid_t>().ok())
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    let _ = pgrp;
    let starttime = fields
        .nth(15)
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    let starttime = std::str::from_utf8(starttime)
        .ok()
        .and_then(|value| value.parse().ok())
        .ok_or_else(|| system_error("parse(/proc/pid/stat)"))?;
    Ok(Some(ProcessStat {
        starttime,
        state,
        parent,
        session,
    }))
}

fn process_stat(pid: libc::pid_t) -> Result<Option<ProcessStat>, ControllerError> {
    process_stat_at(format!("/proc/{pid}/stat"))
}

fn thread_stat(pid: libc::pid_t, tid: libc::pid_t) -> Result<Option<ProcessStat>, ControllerError> {
    process_stat_at(format!("/proc/{pid}/task/{tid}/stat"))
}

fn process_starttime(pid: libc::pid_t) -> Result<Option<u64>, ControllerError> {
    Ok(process_stat(pid)?.map(|stat| stat.starttime))
}

fn process_is_live(fd: RawFd, identity: ProcessIdentity) -> Result<bool, ControllerError> {
    if pidfd_probe(fd)? == PidfdProbe::Gone {
        return Ok(false);
    }
    Ok(matches!(
        process_stat(identity.pid)?,
        Some(stat) if stat.starttime == identity.starttime && stat.state != b'Z'
    ))
}

fn read_bounded_path(
    path: String,
    limit: usize,
    operation: &'static str,
) -> Result<Vec<u8>, ControllerError> {
    let mut file = match File::open(path) {
        Ok(file) => file,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err(system_error(operation)),
    };
    let mut bytes = Vec::with_capacity(4096);
    let mut buffer = [0u8; 4096];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|_| system_error(operation))?;
        if count == 0 {
            break;
        }
        if bytes.len().saturating_add(count) > limit {
            return Err(ControllerError::BudgetExceeded);
        }
        bytes.extend_from_slice(&buffer[..count]);
    }
    Ok(bytes)
}

fn parse_children(bytes: &[u8]) -> Result<Vec<libc::pid_t>, ControllerError> {
    let mut children = Vec::new();
    for token in bytes.split(|byte| byte.is_ascii_whitespace()) {
        if token.is_empty() {
            continue;
        }
        let pid = std::str::from_utf8(token)
            .ok()
            .and_then(|value| value.parse::<libc::pid_t>().ok())
            .filter(|pid| *pid > 0)
            .ok_or_else(|| system_error("parse(/proc/task/children)"))?;
        children.push(pid);
        if children.len() > MAX_CENSUS_PROCESSES {
            return Err(ControllerError::BudgetExceeded);
        }
    }
    Ok(children)
}

fn parse_cgroup_procs(bytes: &[u8]) -> Result<Vec<libc::pid_t>, ControllerError> {
    let mut pids = Vec::new();
    for token in bytes.split(|byte| byte.is_ascii_whitespace()) {
        if token.is_empty() {
            continue;
        }
        let pid = std::str::from_utf8(token)
            .ok()
            .and_then(|value| value.parse::<libc::pid_t>().ok())
            .filter(|pid| *pid > 0)
            .ok_or_else(|| system_error("parse(cgroup.procs)"))?;
        pids.push(pid);
        if pids.len() > MAX_CGROUP_PROCS {
            return Err(ControllerError::BudgetExceeded);
        }
    }
    Ok(pids)
}

fn task_ids(pid: libc::pid_t) -> Result<Vec<libc::pid_t>, ControllerError> {
    let path = format!("/proc/{pid}/task");
    let entries = std::fs::read_dir(path).map_err(|_| system_error("read(/proc/pid/task)"))?;
    let mut tids = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|_| system_error("read(/proc/pid/task)"))?;
        let tid = entry
            .file_name()
            .to_string_lossy()
            .parse::<libc::pid_t>()
            .map_err(|_| system_error("parse(/proc/pid/task)"))?;
        tids.push(tid);
        if tids.len() > MAX_CENSUS_PROCESSES {
            return Err(ControllerError::BudgetExceeded);
        }
    }
    tids.sort_unstable();
    Ok(tids)
}

fn children_of_tid(
    pid: libc::pid_t,
    tid: libc::pid_t,
) -> Result<Vec<libc::pid_t>, ControllerError> {
    let bytes = read_bounded_path(
        format!("/proc/{pid}/task/{tid}/children"),
        MAX_CHILDREN_BYTES,
        "read(/proc/task/children)",
    )?;
    parse_children(&bytes)
}

#[derive(Debug)]
struct CgroupAuthority {
    fd: OwnedFd,
    identity: FileIdentity,
    path: String,
}

fn process_cgroup_path(pid: libc::pid_t) -> Result<Option<String>, ControllerError> {
    let bytes = read_bounded_path(
        format!("/proc/{pid}/cgroup"),
        MAX_FILE_BYTES,
        "read(/proc/pid/cgroup)",
    )?;
    if bytes.is_empty() {
        return Ok(None);
    }
    for line in bytes.split(|byte| *byte == b'\n') {
        let mut fields = line.splitn(3, |byte| *byte == b':');
        let hierarchy = fields.next();
        let controllers = fields.next();
        let path = fields.next();
        if hierarchy == Some(b"0".as_slice()) && controllers == Some(b"".as_slice()) {
            let path = path.ok_or_else(|| system_error("parse(/proc/pid/cgroup)"))?;
            let path =
                std::str::from_utf8(path).map_err(|_| system_error("parse(/proc/pid/cgroup)"))?;
            if !path.starts_with('/')
                || path
                    .split('/')
                    .any(|part| part == ".." || part.contains('\0'))
            {
                return Err(ControllerError::IdentityMismatch(
                    crate::controller::RecoveryObligation::MembershipChanged,
                ));
            }
            return Ok(Some(path.to_owned()));
        }
    }
    Err(system_error("parse(/proc/pid/cgroup)"))
}

fn open_cgroup_authority(pid: libc::pid_t) -> Result<CgroupAuthority, ControllerError> {
    let path = process_cgroup_path(pid)?.ok_or(ControllerError::MissingPidfd)?;
    let root = File::open(std::str::from_utf8(CGROUP_ROOT).unwrap())
        .map_err(|_| system_error("open(cgroup2-root)"))?;
    let fd: OwnedFd = root.into();
    let cgroup = if path == "/" {
        duplicate(fd.as_raw_fd())?
    } else {
        open_relative_path(
            fd.as_raw_fd(),
            path.trim_start_matches('/').as_bytes(),
            libc::O_RDONLY,
        )?
    };
    let identity = identity(cgroup.as_raw_fd())?;
    if mode(identity) != libc::S_IFDIR {
        return Err(ControllerError::IdentityMismatch(
            crate::controller::RecoveryObligation::MembershipChanged,
        ));
    }
    Ok(CgroupAuthority {
        fd: cgroup,
        identity,
        path,
    })
}

fn cgroup_matches(authority: &CgroupAuthority, pid: libc::pid_t) -> Result<(), ControllerError> {
    if process_cgroup_path(pid)?.as_deref() != Some(authority.path.as_str())
        || identity(authority.fd.as_raw_fd())? != authority.identity
    {
        return Err(ControllerError::IdentityMismatch(
            crate::controller::RecoveryObligation::MembershipChanged,
        ));
    }
    Ok(())
}

fn cgroup_session_members(
    authority: &CgroupAuthority,
    session_id: libc::pid_t,
    retained: &[ProcessIdentity],
    subreaper: Option<ProcessIdentity>,
) -> Result<Vec<ProcessIdentity>, ControllerError> {
    let procs = openat(
        authority.fd.as_raw_fd(),
        b"cgroup.procs",
        libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
    )?;
    let pids = parse_cgroup_procs(&read_fd(procs.as_raw_fd(), MAX_CHILDREN_BYTES)?)?;
    let mut members = BTreeMap::new();
    for pid in pids {
        let Some(stat) = process_stat(pid)? else {
            // A process can exit between the kernel's cgroup snapshot and the
            // /proc read.  The next fixed-point round will make this absence
            // authoritative; it is never treated as a live member.
            continue;
        };
        if stat.state == b'Z' {
            // A zombie has no runnable runtime state and cannot be signaled;
            // treating its cgroup entry as live would make a verified kill
            // depend on an unrelated reaper scheduling decision.
            continue;
        }
        let retained_identity = retained
            .iter()
            .find(|identity| identity.pid == pid && identity.starttime == stat.starttime);
        // The normal runtime population is selected by the original SID.  A
        // descendant which called setsid(2) is selected either by its retained
        // (pid,starttime) capability or, before the first capability exists,
        // by reparenting to the launch-time child subreaper.  The latter is
        // restricted to this controller's cgroup and direct child relation;
        // it prevents an early orphan from escaping the first census without
        // treating every same-cgroup process as runtime-owned.
        if stat.session != session_id
            && retained_identity.is_none()
            && subreaper.is_none_or(|identity| stat.parent != identity.pid)
        {
            continue;
        }
        cgroup_matches(authority, pid)?;
        let candidate = ProcessIdentity {
            pid,
            starttime: stat.starttime,
        };
        if let Some(previous) = members.insert(pid, candidate) {
            if previous.starttime != candidate.starttime {
                return Err(ControllerError::IdentityMismatch(
                    crate::controller::RecoveryObligation::PidReuse,
                ));
            }
        }
    }
    Ok(members.into_values().collect())
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct Census {
    members: Vec<ProcessIdentity>,
    tids: BTreeMap<libc::pid_t, BTreeSet<ProcessIdentity>>,
}

fn capture_census(
    root: ProcessIdentity,
    authority: &CgroupAuthority,
    session_id: libc::pid_t,
    retained: &[ProcessIdentity],
    subreaper: Option<ProcessIdentity>,
) -> Result<Census, ControllerError> {
    let mut previous: Option<Census> = None;
    for _round in 0..MAX_CENSUS_ROUNDS {
        if let Some(identity) = subreaper {
            if !subreaper_identity_is_current(identity)? {
                return Err(ControllerError::IdentityMismatch(
                    crate::controller::RecoveryObligation::MembershipChanged,
                ));
            }
        }
        let stat = process_stat(root.pid)?.ok_or(ControllerError::MembershipChanged)?;
        if stat.starttime != root.starttime || stat.state == b'Z' {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::PidReuse,
            ));
        }
        cgroup_matches(authority, root.pid)?;
        if stat.session != session_id {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        let cgroup_members = cgroup_session_members(authority, session_id, retained, subreaper)?;
        if !cgroup_members.contains(&root) {
            return Err(ControllerError::MembershipChanged);
        }
        let mut queue = VecDeque::from([root.pid]);
        let mut visited = BTreeSet::new();
        let mut members = BTreeSet::new();
        let mut tids = BTreeMap::<libc::pid_t, BTreeSet<ProcessIdentity>>::new();
        for candidate in cgroup_members {
            if candidate.pid == root.pid {
                continue;
            }
            members.insert(candidate);
            queue.push_back(candidate.pid);
            if members.len() > MAX_CENSUS_PROCESSES {
                return Err(ControllerError::BudgetExceeded);
            }
        }
        while let Some(pid) = queue.pop_front() {
            if !visited.insert(pid) {
                continue;
            }
            let Some(stat) = process_stat(pid)? else {
                // A cgroup snapshot and /proc are not one atomic read.  A
                // non-root member which exited between the two is no longer
                // a kill target; the retained pidfd and next fixed-point
                // round provide the authoritative disappearance check.
                if pid == root.pid {
                    return Err(ControllerError::MembershipChanged);
                }
                continue;
            };
            cgroup_matches(authority, pid)?;
            let tids_for_process = task_ids(pid)?;
            let mut identities = BTreeSet::new();
            for tid in tids_for_process {
                let Some(thread) = thread_stat(pid, tid)? else {
                    continue;
                };
                identities.insert(ProcessIdentity {
                    pid: tid,
                    starttime: thread.starttime,
                });
                for child in children_of_tid(pid, tid)? {
                    let Some(child_stat) = process_stat(child)? else {
                        continue;
                    };
                    cgroup_matches(authority, child)?;
                    let child_identity = ProcessIdentity {
                        pid: child,
                        starttime: child_stat.starttime,
                    };
                    if child != root.pid {
                        if let Some(existing) = members
                            .iter()
                            .find(|identity: &&ProcessIdentity| identity.pid == child)
                        {
                            if existing.starttime != child_identity.starttime {
                                return Err(ControllerError::IdentityMismatch(
                                    crate::controller::RecoveryObligation::PidReuse,
                                ));
                            }
                        }
                        members.insert(child_identity);
                        if members.len() > MAX_CENSUS_PROCESSES {
                            return Err(ControllerError::BudgetExceeded);
                        }
                        queue.push_back(child);
                    }
                }
            }
            tids.insert(pid, identities);
            let _ = stat;
        }
        let census = Census {
            members: members.into_iter().collect(),
            tids,
        };
        if previous.as_ref() == Some(&census) {
            return Ok(census);
        }
        previous = Some(census);
    }
    Err(ControllerError::BudgetExceeded)
}

#[cfg(test)]
fn read_children(root: ProcessIdentity) -> Result<Vec<ProcessIdentity>, ControllerError> {
    let authority = open_cgroup_authority(root.pid)?;
    let session = process_stat(root.pid)?
        .ok_or(ControllerError::MembershipChanged)?
        .session;
    let subreaper = ProcessIdentity {
        pid: unsafe { libc::getpid() },
        starttime: process_starttime(unsafe { libc::getpid() })?
            .ok_or(ControllerError::MembershipChanged)?,
    };
    Ok(capture_census(root, &authority, session, &[], Some(subreaper))?.members)
}

fn child_subreaper_enabled() -> Result<bool, ControllerError> {
    let mut enabled: libc::c_int = 0;
    // SAFETY: PR_GET_CHILD_SUBREAPER writes one integer to the supplied
    // pointer and does not inspect any borrowed memory beyond it.
    let result = unsafe { libc::prctl(libc::PR_GET_CHILD_SUBREAPER, &mut enabled) };
    if result < 0 {
        return Err(system_error("prctl(PR_GET_CHILD_SUBREAPER)"));
    }
    Ok(enabled != 0)
}

fn subreaper_identity_is_current(identity: ProcessIdentity) -> Result<bool, ControllerError> {
    Ok(child_subreaper_enabled()? && process_starttime(identity.pid)? == Some(identity.starttime))
}

fn parse_pid(bytes: &[u8]) -> Result<libc::pid_t, ControllerError> {
    let value = std::str::from_utf8(bytes)
        .map_err(|_| ControllerError::MalformedRequest)?
        .trim();
    if value.is_empty() {
        return Err(ControllerError::MalformedRequest);
    }
    value
        .parse::<libc::pid_t>()
        .ok()
        .filter(|pid| *pid > 0)
        .ok_or(ControllerError::MalformedRequest)
}

fn validate_owned(
    identity: FileIdentity,
    expected_type: u32,
    expected_mode: Option<u32>,
) -> Result<(), ControllerError> {
    if mode(identity) != expected_type
        || identity.nlink != 1
        || identity.uid != unsafe { libc::geteuid() }
        || expected_mode.is_some_and(|bits| identity.mode & 0o777 != bits)
    {
        return Err(ControllerError::IdentityMismatch(
            crate::controller::RecoveryObligation::EndpointReplacement,
        ));
    }
    Ok(())
}

fn acquire_exclusive(fd: RawFd, timeout: Duration) -> Result<(), ControllerError> {
    let deadline = Instant::now() + timeout;
    loop {
        // SAFETY: fd is an owned regular-file descriptor and LOCK_NB makes
        // the bounded acquisition explicit.
        if unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) } == 0 {
            return Ok(());
        }
        let errno = io::Error::last_os_error().raw_os_error();
        if errno != Some(libc::EWOULDBLOCK) && errno != Some(libc::EAGAIN) {
            return Err(ControllerError::QuiescenceRequired);
        }
        if Instant::now() >= deadline {
            return Err(ControllerError::QuiescenceRequired);
        }
        thread::sleep(Duration::from_millis(2));
    }
}

fn rename_exchange(
    source_parent: RawFd,
    source: &CString,
    destination_parent: RawFd,
    destination: &CString,
) -> Result<(), ControllerError> {
    // RENAME_EXCHANGE atomically swaps the named endpoint with a private
    // placeholder.  If a writer replaces the public name in the final
    // syscall window, the replacement is moved to quarantine and can be
    // exchanged back without ever being unlinked.
    let result = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            source_parent,
            source.as_ptr(),
            destination_parent,
            destination.as_ptr(),
            2u32,
        ) as libc::c_int
    };
    if result < 0 {
        return Err(system_error("renameat2(RENAME_EXCHANGE)"));
    }
    Ok(())
}

fn rename_noreplace(
    source_parent: RawFd,
    source: &CString,
    destination_parent: RawFd,
    destination: &CString,
) -> Result<(), ControllerError> {
    let result = unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            source_parent,
            source.as_ptr(),
            destination_parent,
            destination.as_ptr(),
            1u32,
        ) as libc::c_int
    };
    if result < 0 {
        return Err(system_error("renameat2(RENAME_NOREPLACE)"));
    }
    Ok(())
}

fn session_token(
    root: ProcessIdentity,
    session_id: libc::pid_t,
    authority: &CgroupAuthority,
) -> Vec<u8> {
    format!(
        "{}:{}:{}:{}:{}:{}\n",
        root.pid,
        root.starttime,
        session_id,
        authority.identity.device,
        authority.identity.inode,
        authority.path
    )
    .into_bytes()
}

/// Fixed-name layout used only by task-owned fixtures.  No caller-provided
/// pathname is accepted after the anchor has been acquired.
#[derive(Clone, Copy, Debug, Default)]
pub struct LinuxFixtureLayout;

impl LinuxFixtureLayout {
    pub fn marker_name(self) -> &'static [u8] {
        MARKER_NAME
    }
    pub fn launcher_name(self) -> &'static [u8] {
        LAUNCHER_NAME
    }
    pub fn init_pid_name(self) -> &'static [u8] {
        INIT_PID_NAME
    }
    pub fn run_dir_name(self) -> &'static [u8] {
        RUN_DIR_NAME
    }
    pub fn session_name(self) -> &'static [u8] {
        SESSION_NAME
    }
}

/// Concrete Linux capability backend.  The backend is intentionally not
/// selected by a production factory; tests instantiate it against a private
/// fixture prefix and drive the normal Rust controller transitions.
pub struct LinuxBackend {
    layout: LinuxFixtureLayout,
    anchor_guard: Option<OwnedFd>,
    marker_guard: Option<OwnedFd>,
    marker_identity: Option<FileIdentity>,
    marker_digest: Option<[u8; 32]>,
    root_identity: Option<ProcessIdentity>,
    root_parent: Option<ProcessIdentity>,
    session_id: Option<libc::pid_t>,
    cgroup: Option<CgroupAuthority>,
    session_fd: Option<OwnedFd>,
    session_identity: Option<FileIdentity>,
    session_token: Option<Vec<u8>>,
    init_pid_fd: Option<OwnedFd>,
    init_pid_identity: Option<FileIdentity>,
    initial_census: Option<Census>,
    subreaper_identity: Option<ProcessIdentity>,
    pre_signal_verified: bool,
    root_pidfd: Option<OwnedFd>,
    member_pidfds: Vec<OwnedFd>,
    member_identities: Vec<ProcessIdentity>,
    lock_fd: Option<OwnedFd>,
    lock_identity: Option<FileIdentity>,
    run_dir: Option<OwnedFd>,
    quarantine_dir: Option<OwnedFd>,
    #[cfg(test)]
    replace_shellspawn_after_verify: bool,
    #[cfg(test)]
    replace_placeholder_after_verify: bool,
    #[cfg(test)]
    replace_quarantine_after_verify: bool,
    #[cfg(test)]
    fail_after_quarantine_exchange: bool,
    #[cfg(test)]
    invalidate_lease_after_verify: bool,
    #[cfg(test)]
    replacement_fired: bool,
    stop_barrier_active: bool,
    #[cfg(test)]
    fail_after_root_stop: bool,
    #[cfg(test)]
    fail_resume: bool,
    barrier_obligations: Vec<crate::controller::RecoveryObligation>,
    quarantine_ledger: Vec<QuarantineObligation>,
}

impl Default for LinuxBackend {
    fn default() -> Self {
        Self {
            layout: LinuxFixtureLayout,
            anchor_guard: None,
            marker_guard: None,
            marker_identity: None,
            marker_digest: None,
            root_identity: None,
            root_parent: None,
            session_id: None,
            cgroup: None,
            session_fd: None,
            session_identity: None,
            session_token: None,
            init_pid_fd: None,
            init_pid_identity: None,
            initial_census: None,
            subreaper_identity: None,
            pre_signal_verified: false,
            root_pidfd: None,
            member_pidfds: Vec::new(),
            member_identities: Vec::new(),
            lock_fd: None,
            lock_identity: None,
            run_dir: None,
            quarantine_dir: None,
            #[cfg(test)]
            replace_shellspawn_after_verify: false,
            #[cfg(test)]
            replace_placeholder_after_verify: false,
            #[cfg(test)]
            replace_quarantine_after_verify: false,
            #[cfg(test)]
            fail_after_quarantine_exchange: false,
            #[cfg(test)]
            invalidate_lease_after_verify: false,
            #[cfg(test)]
            replacement_fired: false,
            stop_barrier_active: false,
            #[cfg(test)]
            fail_after_root_stop: false,
            #[cfg(test)]
            fail_resume: false,
            barrier_obligations: Vec::new(),
            quarantine_ledger: Vec::new(),
        }
    }
}

impl LinuxBackend {
    pub fn new(layout: LinuxFixtureLayout) -> Self {
        let mut backend = Self::default();
        backend.layout = layout;
        backend
    }

    pub fn launcher_digest(bytes: &[u8]) -> [u8; 32] {
        digest(bytes)
    }

    pub fn marker_digest(bytes: &[u8]) -> [u8; 32] {
        digest(bytes)
    }

    pub fn membership_snapshot(&self) -> Result<MembershipSnapshot, ControllerError> {
        self.revalidate_lease()?;
        self.revalidate_init_pid()?;
        let root = self.root_identity.ok_or(ControllerError::MissingPidfd)?;
        let authority = self.cgroup.as_ref().ok_or(ControllerError::MissingPidfd)?;
        let session_id = self.session_id.ok_or(ControllerError::MissingPidfd)?;
        let census = capture_census(
            root,
            authority,
            session_id,
            &self.member_identities,
            self.subreaper_identity,
        )?;
        if let Some(initial) = self.initial_census.as_ref() {
            if census != *initial {
                let obligation = if census.members.iter().any(|candidate| {
                    self.member_identities.iter().any(|known| {
                        known.pid == candidate.pid && known.starttime != candidate.starttime
                    })
                }) {
                    crate::controller::RecoveryObligation::PidReuse
                } else {
                    crate::controller::RecoveryObligation::LateFork
                };
                return Err(ControllerError::IdentityMismatch(obligation));
            }
        }
        let members = census.members;
        Ok(MembershipSnapshot {
            source: MembershipSource::RustCgroupAndProcTaskChildren,
            complete: true,
            root: Some(root),
            members,
        })
    }

    pub fn wait_until_empty(&self, timeout: Duration) -> Result<(), ControllerError> {
        self.revalidate_lease()?;
        let deadline = Instant::now() + timeout;
        loop {
            self.revalidate_lease()?;
            let root_alive = self.root_pidfd.as_ref().map_or(Ok(false), |fd| {
                self.root_identity.map_or(Ok(false), |identity| {
                    process_is_live(fd.as_raw_fd(), identity)
                })
            })?;
            let member_alive = self
                .member_pidfds
                .iter()
                .zip(&self.member_identities)
                .map(|(fd, identity)| process_is_live(fd.as_raw_fd(), *identity))
                .collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .any(|live| live);
            let session_id = self.session_id.ok_or(ControllerError::MissingPidfd)?;
            let subreaper = self.revalidate_subreaper()?;
            let cgroup_members = cgroup_session_members(
                self.cgroup.as_ref().ok_or(ControllerError::MissingPidfd)?,
                session_id,
                &self.member_identities,
                Some(subreaper),
            )?;
            if !root_alive && !member_alive && cgroup_members.is_empty() {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(ControllerError::MembershipChanged);
            }
            thread::sleep(Duration::from_millis(5));
        }
    }

    fn endpoint_parent(&self, prefix: &PrefixCapability, kind: EndpointKind) -> RawFd {
        if kind == EndpointKind::InitPid {
            prefix.raw_fd()
        } else {
            self.run_dir
                .as_ref()
                .expect("LinuxBackend retains run directory after acquisition")
                .as_raw_fd()
        }
    }

    fn endpoint_name(kind: EndpointKind) -> &'static [u8] {
        match kind {
            EndpointKind::InitPid => INIT_PID_NAME,
            EndpointKind::DarlingServer => b"darlingserver.sock",
            EndpointKind::Shellspawn => b"shellspawn.sock",
            EndpointKind::Launchd => b"launchd.sock",
        }
    }

    fn cleanup_failure(
        &mut self,
        removed: usize,
        obligation: crate::controller::RecoveryObligation,
    ) -> CleanupFailure {
        CleanupFailure {
            removed,
            obligation,
            quarantines: std::mem::take(&mut self.quarantine_ledger),
        }
    }

    fn revalidate_subreaper(&self) -> Result<ProcessIdentity, ControllerError> {
        let identity = self
            .subreaper_identity
            .ok_or(ControllerError::MembershipChanged)?;
        if !subreaper_identity_is_current(identity)? {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        Ok(identity)
    }

    fn revalidate_lease(&self) -> Result<(), ControllerError> {
        let anchor = self
            .anchor_guard
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        let lock = self
            .lock_fd
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        let expected = self
            .lock_identity
            .ok_or(ControllerError::QuiescenceRequired)?;
        let lock_name = CString::new(LOCK_NAME).map_err(|_| system_error("lock name"))?;
        if identity(lock.as_raw_fd())? != expected
            || FileIdentity::from_at(anchor.as_raw_fd(), &lock_name)
                .map_err(|_| system_error("fstatat(lock)"))?
                != expected
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::QuiescenceRequired,
            ));
        }
        let marker = self
            .marker_guard
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        let marker_identity = self
            .marker_identity
            .ok_or(ControllerError::QuiescenceRequired)?;
        let marker_digest = self
            .marker_digest
            .ok_or(ControllerError::QuiescenceRequired)?;
        let marker_name = CString::new(MARKER_NAME).map_err(|_| system_error("marker name"))?;
        if identity(marker.as_raw_fd())? != marker_identity
            || FileIdentity::from_at(anchor.as_raw_fd(), &marker_name)
                .map_err(|_| system_error("fstatat(marker)"))?
                != marker_identity
            || digest(&read_fd(
                duplicate(marker.as_raw_fd())?.as_raw_fd(),
                MAX_FILE_BYTES,
            )?) != marker_digest
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MarkerIdentity,
            ));
        }
        let session_fd = self
            .session_fd
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        let session_identity = self
            .session_identity
            .ok_or(ControllerError::QuiescenceRequired)?;
        let expected_token = self
            .session_token
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        if identity(session_fd.as_raw_fd())? != session_identity
            || FileIdentity::from_at(
                anchor.as_raw_fd(),
                &CString::new(SESSION_NAME).map_err(|_| system_error("session name"))?,
            )
            .map_err(|_| system_error("fstatat(session)"))?
                != session_identity
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        let session_copy = duplicate(session_fd.as_raw_fd())?;
        if read_fd(session_copy.as_raw_fd(), MAX_FILE_BYTES)? != *expected_token {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        if let Some(cgroup) = self.cgroup.as_ref() {
            if identity(cgroup.fd.as_raw_fd())? != cgroup.identity {
                return Err(ControllerError::IdentityMismatch(
                    crate::controller::RecoveryObligation::MembershipChanged,
                ));
            }
        } else {
            return Err(ControllerError::QuiescenceRequired);
        }
        Ok(())
    }

    /// Validate the retained `.init.pid` authority while its public name is
    /// still expected to refer to the retained inode.  Cleanup deliberately
    /// replaces that name with a private placeholder, so this check is kept
    /// separate from the generic lease check and is called at each
    /// pre-mutation boundary.
    fn revalidate_init_pid(&self) -> Result<(), ControllerError> {
        let anchor = self
            .anchor_guard
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        let init_pid_fd = self
            .init_pid_fd
            .as_ref()
            .ok_or(ControllerError::QuiescenceRequired)?;
        let init_pid_identity = self
            .init_pid_identity
            .ok_or(ControllerError::QuiescenceRequired)?;
        let root_pid = self.root_identity.ok_or(ControllerError::MissingPidfd)?.pid;
        let init_name = CString::new(INIT_PID_NAME).map_err(|_| system_error("init pid name"))?;
        if identity(init_pid_fd.as_raw_fd())? != init_pid_identity
            || FileIdentity::from_at(anchor.as_raw_fd(), &init_name)
                .map_err(|_| system_error("fstatat(init pid)"))?
                != init_pid_identity
            || parse_pid(&read_fd(
                duplicate(init_pid_fd.as_raw_fd())?.as_raw_fd(),
                64,
            )?)? != root_pid
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        Ok(())
    }

    fn verify_census_before_signal(&self) -> Result<(), ControllerError> {
        self.revalidate_init_pid()?;
        let root = self.root_identity.ok_or(ControllerError::MissingPidfd)?;
        let parent = self.root_parent.ok_or(ControllerError::MissingPidfd)?;
        let root_stat = process_stat(root.pid)?.ok_or(ControllerError::MembershipChanged)?;
        if root_stat.parent != parent.pid
            || process_starttime(parent.pid)? != Some(parent.starttime)
            || self.session_id != Some(root_stat.session)
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        let authority = self.cgroup.as_ref().ok_or(ControllerError::MissingPidfd)?;
        let session_id = self.session_id.ok_or(ControllerError::MissingPidfd)?;
        let observed = capture_census(
            root,
            authority,
            session_id,
            &self.member_identities,
            self.subreaper_identity,
        )?;
        let initial = self
            .initial_census
            .as_ref()
            .ok_or(ControllerError::MissingPidfd)?;
        if observed == *initial {
            Ok(())
        } else if observed.members.iter().any(|candidate| {
            initial
                .members
                .iter()
                .any(|known| known.pid == candidate.pid && known.starttime != candidate.starttime)
        }) {
            Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::PidReuse,
            ))
        } else {
            Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::LateFork,
            ))
        }
    }

    fn resume_stopped(&mut self) -> Result<(), ControllerError> {
        let mut all_safe = true;
        if let (Some(root), Some(identity)) = (self.root_pidfd.as_ref(), self.root_identity) {
            if self.resume_one(root.as_raw_fd(), identity) == ResumeOutcome::Failed {
                all_safe = false;
            }
        }
        for (fd, identity) in self
            .member_pidfds
            .iter()
            .zip(self.member_identities.iter().copied())
        {
            if self.resume_one(fd.as_raw_fd(), identity) == ResumeOutcome::Failed {
                all_safe = false;
            }
        }
        if all_safe {
            self.stop_barrier_active = false;
            Ok(())
        } else {
            self.stop_barrier_active = true;
            self.record_barrier_obligation();
            Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::StoppedProcess,
            ))
        }
    }

    fn resume_one(&self, fd: RawFd, identity: ProcessIdentity) -> ResumeOutcome {
        #[cfg(test)]
        if self.fail_resume {
            return ResumeOutcome::Failed;
        }
        match pidfd_resume(fd) {
            Ok(ResumeOutcome::Resumed) => {
                let deadline = Instant::now() + RESUME_WAIT;
                loop {
                    match process_stat(identity.pid) {
                        Ok(None) => return ResumeOutcome::Gone,
                        Ok(Some(stat)) if stat.starttime != identity.starttime => {
                            return ResumeOutcome::Gone;
                        }
                        Ok(Some(stat)) if stat.state != b'T' && stat.state != b't' => {
                            return ResumeOutcome::Resumed;
                        }
                        Ok(Some(_)) if Instant::now() < deadline => {
                            thread::sleep(Duration::from_millis(1));
                        }
                        Ok(Some(_)) => {
                            return match pidfd_force_kill(fd) {
                                Ok(outcome) => outcome,
                                Err(_) => ResumeOutcome::Failed,
                            }
                        }
                        Err(_) => return ResumeOutcome::Failed,
                    }
                }
            }
            Ok(outcome) => outcome,
            Err(_) => force_after_resume_failure(fd),
        }
    }

    fn stopped_process_error(&mut self, original: ControllerError) -> ControllerError {
        if !self.stop_barrier_active {
            return original;
        }
        // Keep the initiating error as the primary result.  If any retained
        // pidfd could not be proven RESUMED, GONE, or KILLED, resume_stopped
        // records a second typed obligation which the controller attaches to
        // the same fail-closed response.
        let _ = self.resume_stopped();
        original
    }

    fn record_barrier_obligation(&mut self) {
        let obligation = crate::controller::RecoveryObligation::StoppedProcess;
        if !self.barrier_obligations.contains(&obligation) {
            self.barrier_obligations.push(obligation);
        }
    }

    fn establish_stop_barrier(&mut self) -> Result<(), ControllerError> {
        let result = (|| {
            self.verify_census_before_signal()?;
            let root = self
                .root_pidfd
                .as_ref()
                .ok_or(ControllerError::MissingPidfd)?;
            pidfd_control_signal(root.as_raw_fd(), libc::SIGSTOP)?;
            self.stop_barrier_active = true;
            #[cfg(test)]
            if self.fail_after_root_stop {
                return Err(system_error("injected barrier failure"));
            }
            // A root stop prevents new children from appearing from the root
            // thread group.  Stop every retained descendant, then take a
            // second fixed-point census so a fork from a descendant during
            // the barrier is rejected rather than silently omitted.
            self.verify_census_before_signal()?;
            for fd in &self.member_pidfds {
                pidfd_control_signal(fd.as_raw_fd(), libc::SIGSTOP)?;
            }
            self.verify_census_before_signal()
        })();
        if let Err(error) = result {
            return Err(self.stopped_process_error(error));
        }
        Ok(())
    }
}

impl Drop for LinuxBackend {
    fn drop(&mut self) {
        if self.stop_barrier_active {
            let _ = self.resume_stopped();
        }
    }
}

impl sealed::CapabilityInspector for LinuxBackend {}

impl CapabilityInspector for LinuxBackend {
    fn acquire_from_anchor(
        &mut self,
        anchor: AnchorCapabilities,
        max_members: usize,
    ) -> Result<AcquisitionBundle, ControllerError> {
        if self.root_identity.is_some() {
            return Err(ControllerError::MalformedRequest);
        }
        let anchor_fd = anchor.raw_fd();
        let anchor_guard = duplicate(anchor_fd)?;

        // The lease is acquired before any runtime observation.  The retained
        // descriptor and the named inode are bound together immediately, so a
        // later unlink/recreate cannot create a split-lock transaction.
        let lock_fd = open_regular(anchor_fd, LOCK_NAME, true)?;
        let lock_identity = identity(lock_fd.as_raw_fd())?;
        validate_owned(lock_identity, libc::S_IFREG, Some(0o600))?;
        acquire_exclusive(lock_fd.as_raw_fd(), Duration::from_secs(1))?;
        let lock_name = CString::new(LOCK_NAME).map_err(|_| system_error("lock name"))?;
        if FileIdentity::from_at(anchor_fd, &lock_name)
            .map_err(|_| system_error("fstatat(lock)"))?
            != lock_identity
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::QuiescenceRequired,
            ));
        }

        let marker_fd =
            open_regular(anchor_fd, self.layout.marker_name(), false).map_err(|_| {
                ControllerError::IdentityMismatch(
                    crate::controller::RecoveryObligation::MarkerIdentity,
                )
            })?;
        let marker_identity = identity(marker_fd.as_raw_fd())?;
        if mode(marker_identity) != libc::S_IFREG
            || marker_identity.nlink != 1
            || marker_identity.uid != unsafe { libc::geteuid() }
            || marker_identity.mode & 0o777 != 0o600
        {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MarkerIdentity,
            ));
        }
        let marker_bytes = read_fd(marker_fd.as_raw_fd(), MAX_FILE_BYTES)?;
        if marker_bytes != MARKER_VALUE {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MarkerIdentity,
            ));
        }
        let marker_guard = duplicate(marker_fd.as_raw_fd())?;
        let launcher_fd = open_regular(anchor_fd, self.layout.launcher_name(), false)?;
        let launcher_identity = identity(launcher_fd.as_raw_fd())?;
        validate_owned(launcher_identity, libc::S_IFREG, None)?;
        let launcher_bytes = read_fd(launcher_fd.as_raw_fd(), MAX_FILE_BYTES)?;

        let pid_file = open_regular(anchor_fd, self.layout.init_pid_name(), false)?;
        let pid_file_identity = identity(pid_file.as_raw_fd())?;
        validate_owned(pid_file_identity, libc::S_IFREG, Some(0o600))?;
        let root_pid = parse_pid(&read_fd(pid_file.as_raw_fd(), 64)?)?;
        let root_stat = process_stat(root_pid)?.ok_or(ControllerError::MissingPidfd)?;
        let root_starttime = root_stat.starttime;
        let root_identity = ProcessIdentity {
            pid: root_pid,
            starttime: root_starttime,
        };
        let controller_pid = unsafe { libc::getpid() };
        if root_stat.parent != controller_pid {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        // A runtime may fork a descendant which calls setsid(2) and then
        // orphan it before the first census.  The controller must already be
        // a child subreaper before launch so that such a member is reparented
        // to this controller and remains observable.  Acquiring this property
        // after the fact would leave an unobservable escape window, so the
        // backend fails closed when the launch-time capability is absent.
        let subreaper_identity = ProcessIdentity {
            pid: controller_pid,
            starttime: process_starttime(controller_pid)?
                .ok_or(ControllerError::MembershipChanged)?,
        };
        if !subreaper_identity_is_current(subreaper_identity)? {
            return Err(ControllerError::MembershipChanged);
        }
        let authority = open_cgroup_authority(root_pid)?;
        let session_fd = open_regular(anchor_fd, SESSION_NAME, false)?;
        let session_identity = identity(session_fd.as_raw_fd())?;
        validate_owned(session_identity, libc::S_IFREG, Some(0o600))?;
        let expected_session_token = session_token(root_identity, root_stat.session, &authority);
        let session_copy = duplicate(session_fd.as_raw_fd())?;
        let observed_session_token = read_fd(session_copy.as_raw_fd(), MAX_FILE_BYTES)?;
        if observed_session_token != expected_session_token {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        let root_fd = pidfd_open(root_pid)?;
        if process_starttime(root_pid)? != Some(root_starttime) {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::PidReuse,
            ));
        }
        let root_guard = duplicate(root_fd.as_raw_fd())?;
        let initial_census = capture_census(
            root_identity,
            &authority,
            root_stat.session,
            &[],
            Some(subreaper_identity),
        )?;
        let initial_children = initial_census.members.clone();
        if initial_children.len() > max_members {
            return Err(ControllerError::BudgetExceeded);
        }
        let mut member_fds = Vec::with_capacity(initial_children.len());
        let mut retained_member_fds = Vec::with_capacity(initial_children.len());
        for identity in &initial_children {
            let fd = pidfd_open(identity.pid)?;
            cgroup_matches(&authority, identity.pid)?;
            if process_starttime(identity.pid)? != Some(identity.starttime) {
                return Err(ControllerError::IdentityMismatch(
                    crate::controller::RecoveryObligation::PidReuse,
                ));
            }
            retained_member_fds.push(duplicate(fd.as_raw_fd())?);
            member_fds.push((fd, *identity));
        }
        let run_dir = open_relative_path(
            anchor_fd,
            self.layout.run_dir_name(),
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )?;
        let quarantine_dir = open_relative_path(
            anchor_fd,
            QUARANTINE_NAME,
            libc::O_RDONLY | libc::O_CLOEXEC | libc::O_NOFOLLOW,
        )?;
        // The endpoint capability is a duplicate of the exact descriptor used
        // to read the PID.  No later pathname open can silently bind cleanup
        // to a replacement inode.
        let pid_endpoint = duplicate(pid_file.as_raw_fd())?;
        let pid_endpoint_identity = identity(pid_endpoint.as_raw_fd())?;
        validate_owned(pid_endpoint_identity, libc::S_IFREG, Some(0o600))?;
        let mut endpoints = vec![(EndpointKind::InitPid, pid_endpoint, pid_endpoint_identity)];
        for (kind, name) in ENDPOINT_NAMES {
            let fd = open_object(run_dir.as_raw_fd(), name)?;
            let identity = identity(fd.as_raw_fd())?;
            validate_owned(identity, libc::S_IFSOCK, Some(0o600))?;
            endpoints.push((*kind, fd, identity));
        }
        let root_parent = ProcessIdentity {
            pid: subreaper_identity.pid,
            starttime: subreaper_identity.starttime,
        };
        self.root_identity = Some(root_identity);
        self.root_parent = Some(root_parent);
        self.session_id = Some(root_stat.session);
        self.anchor_guard = Some(anchor_guard);
        self.marker_guard = Some(marker_guard);
        self.marker_identity = Some(marker_identity);
        self.marker_digest = Some(digest(&marker_bytes));
        self.cgroup = Some(authority);
        self.session_fd = Some(session_fd);
        self.session_identity = Some(session_identity);
        self.session_token = Some(expected_session_token);
        self.subreaper_identity = Some(subreaper_identity);
        self.init_pid_identity = Some(pid_file_identity);
        self.init_pid_fd = Some(pid_file);
        self.initial_census = Some(initial_census);
        self.root_pidfd = Some(root_guard);
        self.member_pidfds = retained_member_fds;
        self.member_identities = initial_children.clone();
        self.lock_fd = Some(lock_fd);
        self.lock_identity = Some(lock_identity);
        self.run_dir = Some(run_dir);
        self.quarantine_dir = Some(quarantine_dir);
        AcquisitionBundle::from_linux_owned(
            anchor,
            marker_fd,
            MarkerObservation {
                identity: marker_identity,
                content_digest: digest(&marker_bytes),
                mode: marker_identity.mode,
                uid: marker_identity.uid,
            },
            launcher_fd,
            LauncherObservation {
                identity: launcher_identity,
                content_digest: digest(&launcher_bytes),
            },
            root_fd,
            root_identity,
            member_fds,
            endpoints,
        )
    }
}

impl sealed::SignalExecutor for LinuxBackend {}

impl SignalExecutor for LinuxBackend {
    fn abort_after_error(&mut self) {
        if self.stop_barrier_active {
            let _ = self.resume_stopped();
        }
    }

    fn take_failure_obligations(&mut self) -> Vec<crate::controller::RecoveryObligation> {
        std::mem::take(&mut self.barrier_obligations)
    }

    fn signal_member(
        &mut self,
        capability: &PidFdCapability,
    ) -> Result<SignalEvidence, ControllerError> {
        self.revalidate_lease()?;
        if !self.pre_signal_verified {
            self.establish_stop_barrier()?;
            self.pre_signal_verified = true;
        }
        let evidence = match pidfd_signal(capability.raw_fd(), capability.target()) {
            Ok(evidence) => evidence,
            Err(error) => {
                return Err(self.stopped_process_error(error));
            }
        };
        let result = match &evidence {
            SignalEvidence::RustPidfd { result, .. } => *result,
            _ => unreachable!("Linux pidfd backend only emits RustPidfd evidence"),
        };
        if result != crate::controller::SignalResult::Sent {
            let _ = self.resume_stopped();
        }
        Ok(match evidence {
            SignalEvidence::RustPidfd { result, .. } => {
                SignalEvidence::rust_pidfd(capability.target(), result)
            }
            _ => unreachable!("Linux pidfd backend only emits RustPidfd evidence"),
        })
    }

    fn signal_root(
        &mut self,
        capability: &PidFdCapability,
    ) -> Result<SignalEvidence, ControllerError> {
        self.revalidate_lease()?;
        let evidence = self.signal_member(capability)?;
        if matches!(
            evidence,
            SignalEvidence::RustPidfd {
                result: crate::controller::SignalResult::Sent,
                ..
            }
        ) {
            // The root is the final member of the barrier.  Once its pidfd
            // accepted SIGKILL, no process in the stopped set remains frozen.
            self.stop_barrier_active = false;
        }
        Ok(evidence)
    }
}

impl sealed::QuiescenceBackend for LinuxBackend {}

impl QuiescenceBackend for LinuxBackend {
    fn prove(&mut self, prefix: &PrefixCapability) -> Result<QuiescenceProof, ControllerError> {
        self.revalidate_lease()?;
        self.revalidate_init_pid()?;
        if identity(prefix.raw_fd())? != prefix.identity() {
            return Err(ControllerError::IdentityMismatch(
                crate::controller::RecoveryObligation::MembershipChanged,
            ));
        }
        let deadline = Instant::now() + Duration::from_secs(1);
        loop {
            self.revalidate_lease()?;
            let root_alive = self.root_pidfd.as_ref().map_or(Ok(false), |fd| {
                self.root_identity.map_or(Ok(false), |identity| {
                    process_is_live(fd.as_raw_fd(), identity)
                })
            })?;
            let member_alive = self
                .member_pidfds
                .iter()
                .zip(&self.member_identities)
                .map(|(fd, identity)| process_is_live(fd.as_raw_fd(), *identity))
                .collect::<Result<Vec<_>, _>>()?
                .into_iter()
                .any(|live| live);
            let session_id = self.session_id.ok_or(ControllerError::MissingPidfd)?;
            let subreaper = self.revalidate_subreaper()?;
            let cgroup_members = cgroup_session_members(
                self.cgroup.as_ref().ok_or(ControllerError::MissingPidfd)?,
                session_id,
                &self.member_identities,
                Some(subreaper),
            )?;
            if !root_alive && !member_alive && cgroup_members.is_empty() {
                self.revalidate_lease()?;
                return Ok(QuiescenceProof::from_prefix(prefix));
            }
            if Instant::now() >= deadline {
                return Err(ControllerError::QuiescenceRequired);
            }
            thread::sleep(Duration::from_millis(5));
        }
    }
}

impl sealed::EndpointCleanupExecutor for LinuxBackend {}

impl EndpointCleanupExecutor for LinuxBackend {
    fn cleanup_fd_relative(
        &mut self,
        prefix: &PrefixCapability,
        endpoints: &mut [EndpointCapability],
        proof: &QuiescenceProof,
    ) -> Result<usize, CleanupFailure> {
        if !proof.validates_prefix(prefix) {
            return Err(
                self.cleanup_failure(0, crate::controller::RecoveryObligation::QuiescenceRequired)
            );
        }
        if self.lock_fd.is_none() || self.quarantine_dir.is_none() {
            return Err(
                self.cleanup_failure(0, crate::controller::RecoveryObligation::QuiescenceRequired)
            );
        }
        if self.revalidate_init_pid().is_err() {
            return Err(
                self.cleanup_failure(0, crate::controller::RecoveryObligation::MembershipChanged)
            );
        }
        let removed = 0;
        let mut staged = 0;
        for (index, endpoint) in endpoints.iter_mut().enumerate() {
            if self.revalidate_lease().is_err() {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                ));
            }
            let expected = endpoint.identity().identity;
            let observed = match identity(endpoint.raw_fd()) {
                Ok(identity) => identity,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            if observed != expected {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            let name = match component(Self::endpoint_name(endpoint.identity().kind)) {
                Ok(name) => name,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            let parent = self.endpoint_parent(prefix, endpoint.identity().kind);
            let named_identity = match FileIdentity::from_at(parent, &name) {
                Ok(identity) => identity,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            if named_identity != expected {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            let quarantine_dir = self.quarantine_dir.as_ref().unwrap().as_raw_fd();
            let source_parent_identity = match identity(parent) {
                Ok(value) => value,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            let quarantine_parent_identity = match identity(quarantine_dir) {
                Ok(value) => value,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            let endpoint_scope = endpoint.scope();
            let writer_parent_identity = identity(prefix.raw_fd()).map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            let quarantine_name = CString::new(format!(
                ".lifecycle-quarantine-{}-{}",
                unsafe { libc::getpid() },
                index
            ))
            .map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                )
            })?;
            let placeholder = match openat(
                quarantine_dir,
                quarantine_name.as_bytes(),
                libc::O_RDWR | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
            ) {
                Ok(fd) => fd,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            let placeholder_identity = match identity(placeholder.as_raw_fd()) {
                Ok(value) => value,
                Err(_) => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::EndpointReplacement,
                    ))
                }
            };
            if self.revalidate_lease().is_err() {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                ));
            }
            #[cfg(test)]
            if self.replace_shellspawn_after_verify
                && !self.replacement_fired
                && endpoint.identity().kind == EndpointKind::Shellspawn
            {
                self.replacement_fired = true;
                // This hook runs after the last lease check and immediately
                // before the atomic exchange.  The exchange protocol must
                // restore this replacement when its identity is observed in
                // the private quarantine.
                let removed_name = unsafe { libc::unlinkat(parent, name.as_ptr(), 0) };
                assert_eq!(removed_name, 0);
                let replacement = unsafe {
                    libc::openat(
                        parent,
                        name.as_ptr(),
                        libc::O_WRONLY
                            | libc::O_CREAT
                            | libc::O_EXCL
                            | libc::O_CLOEXEC
                            | libc::O_NOFOLLOW,
                        0o600,
                    )
                };
                assert!(replacement >= 0);
                let bytes = b"replacement";
                let written = unsafe {
                    libc::write(
                        replacement,
                        bytes.as_ptr().cast::<libc::c_void>(),
                        bytes.len(),
                    )
                };
                assert_eq!(written, bytes.len() as isize);
                unsafe { libc::close(replacement) };
                if self.invalidate_lease_after_verify {
                    let lock_name = component(LOCK_NAME).expect("fixed lock name");
                    let _ = unsafe { libc::unlinkat(prefix.raw_fd(), lock_name.as_ptr(), 0) };
                    let replacement_lock = unsafe {
                        libc::openat(
                            prefix.raw_fd(),
                            lock_name.as_ptr(),
                            libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                            0o600,
                        )
                    };
                    assert!(replacement_lock >= 0);
                    unsafe { libc::close(replacement_lock) };
                }
            }
            // Bind both directory authorities before the first mutation.  A
            // later exchange error can therefore return a complete typed
            // handoff without reopening either parent by name.
            let retained_source_parent = duplicate(parent).map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            let retained_quarantine_parent = duplicate(quarantine_dir).map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            // Retain the exact endpoint object before the first namespace
            // mutation.  If the exchange or any later checkpoint fails, the
            // handoff never needs to reopen a potentially replaced name.
            let retained_endpoint = duplicate(endpoint.raw_fd()).map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            let writer_lock_identity = self.lock_identity.ok_or_else(|| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            let lock_fd = match self.lock_fd.as_ref() {
                Some(fd) => fd.as_raw_fd(),
                None => {
                    return Err(self.cleanup_failure(
                        removed,
                        crate::controller::RecoveryObligation::QuiescenceRequired,
                    ))
                }
            };
            let retained_writer_lock = duplicate(lock_fd).map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            let retained_writer_parent = duplicate(prefix.raw_fd()).map_err(|_| {
                self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                )
            })?;
            if rename_exchange(parent, &name, quarantine_dir, &quarantine_name).is_err() {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            let ledger_index = self.quarantine_ledger.len();
            self.quarantine_ledger.push(QuarantineObligation::new(
                endpoint_scope,
                endpoint.identity().kind,
                expected,
                source_parent_identity,
                quarantine_parent_identity,
                placeholder_identity,
                writer_parent_identity,
                writer_lock_identity,
                retained_source_parent,
                retained_quarantine_parent,
                retained_writer_parent,
                retained_writer_lock,
                LOCK_NAME.to_vec(),
                name.as_bytes().to_vec(),
                quarantine_name.as_bytes().to_vec(),
                name.as_bytes().to_vec(),
                retained_endpoint,
                placeholder,
            ));
            #[cfg(test)]
            if self.fail_after_quarantine_exchange && !self.replacement_fired {
                // This is the first fallible boundary after the exchange.
                // The ledger entry must already own both directory
                // authorities and the placeholder before this injected exit.
                self.replacement_fired = true;
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            let quarantined_identity =
                identity(self.quarantine_ledger[ledger_index].endpoint_fd.as_raw_fd()).ok();
            if quarantined_identity != Some(expected) {
                // Restore the public replacement and leave only our private
                // placeholder behind.  The replacement is never removed.
                let _ = rename_exchange(quarantine_dir, &quarantine_name, parent, &name);
                let obligation = if self.revalidate_lease().is_err() {
                    crate::controller::RecoveryObligation::QuiescenceRequired
                } else {
                    crate::controller::RecoveryObligation::EndpointReplacement
                };
                return Err(self.cleanup_failure(removed, obligation));
            }
            if FileIdentity::from_at(parent, &name).ok() != Some(placeholder_identity)
                || self.revalidate_lease().is_err()
            {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                ));
            }
            #[cfg(test)]
            if self.replace_placeholder_after_verify
                && !self.replacement_fired
                && endpoint.identity().kind == EndpointKind::Shellspawn
            {
                self.replacement_fired = true;
                let removed_name = unsafe { libc::unlinkat(parent, name.as_ptr(), 0) };
                assert_eq!(removed_name, 0);
                let replacement = unsafe {
                    libc::openat(
                        parent,
                        name.as_ptr(),
                        libc::O_WRONLY
                            | libc::O_CREAT
                            | libc::O_EXCL
                            | libc::O_CLOEXEC
                            | libc::O_NOFOLLOW,
                        0o600,
                    )
                };
                assert!(replacement >= 0);
                let bytes = b"placeholder-replacement";
                let written = unsafe {
                    libc::write(
                        replacement,
                        bytes.as_ptr().cast::<libc::c_void>(),
                        bytes.len(),
                    )
                };
                assert_eq!(written, bytes.len() as isize);
                unsafe { libc::close(replacement) };
            }
            let placeholder_gc_name =
                CString::new(format!("{}.public-gc", quarantine_name.to_string_lossy())).map_err(
                    |_| {
                        self.cleanup_failure(
                            removed,
                            crate::controller::RecoveryObligation::QuiescenceRequired,
                        )
                    },
                )?;
            // Move the public placeholder into the private quarantine before
            // handing it to an external GC authority.  This removes the
            // public check-then-unlink window; if a writer won the rename,
            // the moved inode is restored and is never deleted.
            if rename_noreplace(parent, &name, quarantine_dir, &placeholder_gc_name).is_err() {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            if FileIdentity::from_at(quarantine_dir, &placeholder_gc_name).ok()
                != Some(placeholder_identity)
            {
                let _ = rename_noreplace(quarantine_dir, &placeholder_gc_name, parent, &name);
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            self.quarantine_ledger[ledger_index]
                .rename_placeholder(placeholder_gc_name.as_bytes().to_vec());
            if self.revalidate_lease().is_err() {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::QuiescenceRequired,
                ));
            }
            if FileIdentity::from_at(quarantine_dir, &quarantine_name).ok() != Some(expected) {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            #[cfg(test)]
            if self.replace_quarantine_after_verify
                && !self.replacement_fired
                && endpoint.identity().kind == EndpointKind::Shellspawn
            {
                self.replacement_fired = true;
                let removed_name =
                    unsafe { libc::unlinkat(quarantine_dir, quarantine_name.as_ptr(), 0) };
                assert_eq!(removed_name, 0);
                let replacement = unsafe {
                    libc::openat(
                        quarantine_dir,
                        quarantine_name.as_ptr(),
                        libc::O_WRONLY
                            | libc::O_CREAT
                            | libc::O_EXCL
                            | libc::O_CLOEXEC
                            | libc::O_NOFOLLOW,
                        0o600,
                    )
                };
                assert!(replacement >= 0);
                unsafe { libc::close(replacement) };
            }
            let endpoint_gc_name =
                CString::new(format!("{}.endpoint-gc", quarantine_name.to_string_lossy()))
                    .map_err(|_| {
                        self.cleanup_failure(
                            removed,
                            crate::controller::RecoveryObligation::QuiescenceRequired,
                        )
                    })?;
            if rename_noreplace(
                quarantine_dir,
                &quarantine_name,
                quarantine_dir,
                &endpoint_gc_name,
            )
            .is_err()
            {
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            if FileIdentity::from_at(quarantine_dir, &endpoint_gc_name).ok() != Some(expected) {
                let _ = rename_noreplace(
                    quarantine_dir,
                    &endpoint_gc_name,
                    quarantine_dir,
                    &quarantine_name,
                );
                return Err(self.cleanup_failure(
                    removed,
                    crate::controller::RecoveryObligation::EndpointReplacement,
                ));
            }
            self.quarantine_ledger[ledger_index]
                .rename_endpoint(endpoint_gc_name.as_bytes().to_vec());
            // The two exact objects are now retained in the private
            // quarantine.  There is deliberately no fstat->unlink operation
            // here: only a separate quiescent controller with an exclusive
            // namespace-writer authority may GC these names.
            let _ = (placeholder_gc_name, endpoint_gc_name);
            staged += 1;
        }
        if staged != 0 {
            Err(self.cleanup_failure(
                removed,
                crate::controller::RecoveryObligation::QuarantineGcRequired,
            ))
        } else {
            Ok(removed)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::controller::{
        Controller, ControllerBudget, ControllerError, ControllerOperation, ControllerProfile,
        ControllerRequest, RecoveryObligation,
    };
    use std::fs::{create_dir_all, remove_dir_all, set_permissions, Permissions};
    use std::os::unix::fs::PermissionsExt;
    use std::os::unix::net::UnixListener;
    use std::process::{Child, Command, Stdio};
    use std::sync::{Mutex, MutexGuard, OnceLock};
    use std::time::{SystemTime, UNIX_EPOCH};

    static FIXTURE_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

    struct Fixture {
        root: std::path::PathBuf,
        process: Child,
        sockets: Vec<UnixListener>,
        _lock: MutexGuard<'static, ()>,
    }

    impl Fixture {
        fn create() -> Self {
            Self::create_with_script("sleep 30 & wait")
        }

        fn create_with_script(script: &str) -> Self {
            // A subreaper receives every orphaned child of this test process.
            // Serialize fixture lifetimes so a second runtime cannot make an
            // unrelated reparented child indistinguishable from this one.
            let fixture_lock = FIXTURE_LOCK
                .get_or_init(|| Mutex::new(()))
                .lock()
                .expect("fixture lock poisoned");
            let nonce = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos();
            let root = std::env::temp_dir().join(format!("darling-linux-controller-{nonce}"));
            create_dir_all(root.join("var/run")).unwrap();
            create_dir_all(root.join(std::str::from_utf8(QUARANTINE_NAME).unwrap())).unwrap();
            let marker = root.join(std::str::from_utf8(MARKER_NAME).unwrap());
            std::fs::write(&marker, MARKER_VALUE).unwrap();
            set_permissions(&marker, Permissions::from_mode(0o600)).unwrap();
            let launcher_bytes = b"task-owned-launcher-v1\n";
            let launcher = root.join(std::str::from_utf8(LAUNCHER_NAME).unwrap());
            std::fs::write(&launcher, launcher_bytes).unwrap();
            set_permissions(&launcher, Permissions::from_mode(0o700)).unwrap();
            let lock = root.join(std::str::from_utf8(LOCK_NAME).unwrap());
            std::fs::write(&lock, b"fixture-lock\n").unwrap();
            set_permissions(&lock, Permissions::from_mode(0o600)).unwrap();
            install_child_subreaper();
            let process = Command::new("setsid")
                .args(["sh", "-c", script])
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap();
            std::fs::write(
                root.join(std::str::from_utf8(INIT_PID_NAME).unwrap()),
                format!("{}\n", process.id()),
            )
            .unwrap();
            set_permissions(
                root.join(std::str::from_utf8(INIT_PID_NAME).unwrap()),
                Permissions::from_mode(0o600),
            )
            .unwrap();
            let mut sockets = Vec::new();
            for (_, name) in ENDPOINT_NAMES {
                let path = root
                    .join("var/run")
                    .join(std::str::from_utf8(name).unwrap());
                sockets.push(UnixListener::bind(&path).unwrap());
                set_permissions(&path, Permissions::from_mode(0o600)).unwrap();
            }
            let root_identity = ProcessIdentity {
                pid: process.id() as libc::pid_t,
                starttime: process_starttime(process.id() as libc::pid_t)
                    .unwrap()
                    .unwrap(),
            };
            let authority = open_cgroup_authority(root_identity.pid).unwrap();
            let session_deadline = Instant::now() + Duration::from_secs(1);
            let root_session = loop {
                let stat = process_stat(root_identity.pid).unwrap().unwrap();
                if stat.session == root_identity.pid {
                    break stat.session;
                }
                assert!(
                    Instant::now() < session_deadline,
                    "setsid did not establish a session"
                );
                thread::sleep(Duration::from_millis(2));
            };
            std::fs::write(
                root.join(std::str::from_utf8(SESSION_NAME).unwrap()),
                session_token(root_identity, root_session, &authority),
            )
            .unwrap();
            set_permissions(
                root.join(std::str::from_utf8(SESSION_NAME).unwrap()),
                Permissions::from_mode(0o600),
            )
            .unwrap();
            let deadline = Instant::now() + Duration::from_secs(1);
            while Instant::now() < deadline {
                if read_children(root_identity)
                    .map(|children| !children.is_empty())
                    .unwrap_or(false)
                {
                    break;
                }
                thread::sleep(Duration::from_millis(5));
            }
            Self {
                root,
                process,
                sockets,
                _lock: fixture_lock,
            }
        }

        fn launcher_bytes(&self) -> Vec<u8> {
            std::fs::read(self.root.join(std::str::from_utf8(LAUNCHER_NAME).unwrap())).unwrap()
        }
    }

    fn install_child_subreaper() {
        // The fixture must install this before spawning the runtime root.  It
        // is the launch-time membership authority for descendants which call
        // setsid(2) and orphan themselves before the first backend census.
        let result = unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) };
        assert_eq!(result, 0, "fixture requires PR_SET_CHILD_SUBREAPER");
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            if let Ok(Some(starttime)) = process_starttime(self.process.id() as libc::pid_t) {
                let root = ProcessIdentity {
                    pid: self.process.id() as libc::pid_t,
                    starttime,
                };
                if let Ok(children) = read_children(root) {
                    for child in children {
                        if let Ok(fd) = pidfd_open(child.pid) {
                            let _ = pidfd_signal(fd.as_raw_fd(), SignalTarget::SessionMember);
                        }
                    }
                }
            }
            let _ = self.process.kill();
            let _ = self.process.wait();
            self.sockets.clear();
            if let Err(error) = remove_dir_all(&self.root) {
                eprintln!("fixture cleanup {}: {error}", self.root.display());
            }
        }
    }

    fn fixture_request(anchor_fd: RawFd, launcher_digest: [u8; 32]) -> ControllerRequest {
        ControllerRequest {
            schema_version: crate::controller::CONTROLLER_SCHEMA_VERSION,
            transaction_id: "linux-fixture-transaction".into(),
            profile: ControllerProfile::Rootless,
            operation: ControllerOperation::RequestShutdown,
            anchor_fd,
            evidence_fd: None,
            controller_closure_sha256: env!("LIFECYCLE_CONTROLLER_CLOSURE_SHA256").into(),
            runtime_identity_digest: launcher_digest
                .iter()
                .map(|byte| format!("{byte:02x}"))
                .collect(),
            request_nonce: "44".repeat(32),
            budget: ControllerBudget {
                max_events: 32,
                max_virtual_time_ns: 1000,
                max_members: 8,
                max_recovery_steps: 8,
                deadline_ns: 1000,
            },
        }
    }

    #[test]
    fn linux_fixture_runs_real_pidfd_and_fd_relative_cleanup() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        assert!(
            !initial.members.is_empty(),
            "fixture must have a real child"
        );
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let requested = bound.request_shutdown(initial, &mut backend).unwrap();
        backend.wait_until_empty(Duration::from_secs(2)).unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustProcTaskChildren,
            ))
            .unwrap();
        let quiescent = drained.establish_quiescence(&mut backend).unwrap();
        let pending = quiescent.cleanup(&mut backend).unwrap_err();
        assert_eq!(pending.error(), &ControllerError::PartialCleanup);
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::QuarantineGcRequired));
        assert_eq!(
            pending.quarantine_obligations().len(),
            ENDPOINT_NAMES.len() + 1
        );
        let finalized = pending.finalize_fail_closed();
        assert_eq!(
            finalized.quarantine_obligations().len(),
            ENDPOINT_NAMES.len() + 1
        );
        let (response, quarantines) = finalized.into_parts();
        assert_eq!(
            response.verdict,
            crate::controller::ControllerVerdict::FailClosed
        );
        assert_eq!(
            response.obligations,
            vec![RecoveryObligation::QuarantineGcRequired]
        );
        assert_eq!(quarantines.len(), ENDPOINT_NAMES.len() + 1);
        assert!(!fixture.root.join(".init.pid").exists());
        assert!(!fixture.root.join("var/run/darlingserver.sock").exists());
        assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
        assert!(!fixture.root.join("var/run/launchd.sock").exists());
        let quarantine = fixture
            .root
            .join(std::str::from_utf8(QUARANTINE_NAME).unwrap());
        let retained = std::fs::read_dir(quarantine)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| {
                let name = entry.file_name().to_string_lossy().into_owned();
                name.contains(".public-gc") || name.contains(".endpoint-gc")
            })
            .count();
        assert_eq!(retained, ENDPOINT_NAMES.len() * 2 + 2);
    }

    #[test]
    fn linux_fixture_rejects_endpoint_replacement_before_unlink() {
        let fixture = Fixture::create();
        let replacement = fixture.root.join("var/run/shellspawn.sock");
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let requested = bound.request_shutdown(initial, &mut backend).unwrap();
        backend.wait_until_empty(Duration::from_secs(2)).unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustProcTaskChildren,
            ))
            .unwrap();
        let quiescent = drained.establish_quiescence(&mut backend).unwrap();
        backend.replace_shellspawn_after_verify = true;
        let pending = quiescent.cleanup(&mut backend).unwrap_err();
        assert_eq!(pending.error(), &ControllerError::PartialCleanup);
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::EndpointReplacement));
        assert_eq!(pending.quarantine_obligations().len(), ENDPOINT_NAMES.len());
        assert_eq!(std::fs::read(&replacement).unwrap(), b"replacement");
        let finalized = pending.finalize_fail_closed();
        let (response, quarantines) = finalized.into_parts();
        assert_eq!(
            response.verdict,
            crate::controller::ControllerVerdict::FailClosed
        );
        assert_eq!(quarantines.len(), ENDPOINT_NAMES.len());
    }

    #[test]
    fn linux_fixture_registers_quarantine_before_post_exchange_failure() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let requested = bound.request_shutdown(initial, &mut backend).unwrap();
        backend.wait_until_empty(Duration::from_secs(2)).unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustCgroupAndProcTaskChildren,
            ))
            .unwrap();
        let quiescent = drained.establish_quiescence(&mut backend).unwrap();
        backend.fail_after_quarantine_exchange = true;
        let pending = quiescent.cleanup(&mut backend).unwrap_err();
        assert_eq!(pending.quarantine_obligations().len(), 1);
        let obligation = &pending.quarantine_obligations()[0];
        assert_eq!(obligation.kind, EndpointKind::InitPid);
        assert_eq!(
            identity(obligation.endpoint_fd.as_raw_fd()).unwrap(),
            obligation.identity
        );
        assert!(!obligation.source_name.is_empty());
        assert!(!obligation.endpoint_name.is_empty());
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::EndpointReplacement));
        let finalized = pending.finalize_fail_closed();
        let (_response, quarantines) = finalized.into_parts();
        assert_eq!(quarantines.len(), 1);
    }

    #[test]
    fn linux_fixture_preserves_replacement_after_placeholder_verify() {
        let fixture = Fixture::create();
        let replacement = fixture.root.join("var/run/shellspawn.sock");
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let requested = bound.request_shutdown(initial, &mut backend).unwrap();
        backend.wait_until_empty(Duration::from_secs(2)).unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustCgroupAndProcTaskChildren,
            ))
            .unwrap();
        let quiescent = drained.establish_quiescence(&mut backend).unwrap();
        backend.replace_placeholder_after_verify = true;
        let pending = quiescent.cleanup(&mut backend).unwrap_err();
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::EndpointReplacement));
        assert_eq!(pending.quarantine_obligations().len(), ENDPOINT_NAMES.len());
        assert_eq!(
            std::fs::read(&replacement).unwrap(),
            b"placeholder-replacement"
        );
    }

    #[test]
    fn linux_fixture_preserves_replacement_after_quarantine_verify() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let requested = bound.request_shutdown(initial, &mut backend).unwrap();
        backend.wait_until_empty(Duration::from_secs(2)).unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustCgroupAndProcTaskChildren,
            ))
            .unwrap();
        let quiescent = drained.establish_quiescence(&mut backend).unwrap();
        backend.replace_quarantine_after_verify = true;
        let pending = quiescent.cleanup(&mut backend).unwrap_err();
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::EndpointReplacement));
        assert_eq!(pending.quarantine_obligations().len(), ENDPOINT_NAMES.len());
        let quarantine = fixture
            .root
            .join(std::str::from_utf8(QUARANTINE_NAME).unwrap());
        let retained = std::fs::read_dir(quarantine)
            .unwrap()
            .filter_map(Result::ok)
            .filter(|entry| {
                entry
                    .file_name()
                    .to_string_lossy()
                    .contains("lifecycle-quarantine-")
            })
            .count();
        assert!(retained >= 1, "quarantine replacement was not retained");
    }

    #[test]
    fn linux_fixture_rejects_foreign_pid_even_when_starttime_is_valid() {
        let fixture = Fixture::create();
        let pid_path = fixture
            .root
            .join(std::str::from_utf8(INIT_PID_NAME).unwrap());
        std::fs::write(&pid_path, format!("{}\n", unsafe { libc::getpid() })).unwrap();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let error = prepared.acquire(anchor_cap, &mut backend).unwrap_err();
        assert!(matches!(
            error,
            ControllerError::IdentityMismatch(RecoveryObligation::MembershipChanged)
                | ControllerError::IdentityMismatch(RecoveryObligation::PidReuse)
        ));
    }

    #[test]
    fn linux_fixture_census_traverses_grandchildren() {
        let fixture = Fixture::create_with_script("sh -c 'sleep 30' & wait");
        let root_identity = ProcessIdentity {
            pid: fixture.process.id() as libc::pid_t,
            starttime: process_starttime(fixture.process.id() as libc::pid_t)
                .unwrap()
                .unwrap(),
        };
        let deadline = Instant::now() + Duration::from_secs(1);
        let mut members = Vec::new();
        while Instant::now() < deadline {
            members = read_children(root_identity).unwrap();
            if members.len() >= 2 {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            members.len() >= 2,
            "expected a descendant process and grandchild"
        );
    }

    #[test]
    fn linux_fixture_census_includes_orphaned_cgroup_member() {
        // The short-lived intermediate shell exits while its sleep child is
        // still alive.  The child is reparented to the launch-time subreaper,
        // so /proc/task/*/children no longer reaches it; cgroup.procs plus the
        // shared SID must retain it in the authoritative census.
        let fixture = Fixture::create_with_script("(sleep 30 & exit) & sleep 30");
        let root = ProcessIdentity {
            pid: fixture.process.id() as libc::pid_t,
            starttime: process_starttime(fixture.process.id() as libc::pid_t)
                .unwrap()
                .unwrap(),
        };
        let deadline = Instant::now() + Duration::from_secs(1);
        let mut members = Vec::new();
        let mut orphan_seen = false;
        while Instant::now() < deadline {
            members = read_children(root).unwrap();
            orphan_seen = members.iter().any(|member| {
                process_stat(member.pid)
                    .ok()
                    .flatten()
                    .is_some_and(|stat| stat.parent == unsafe { libc::getpid() })
            });
            if orphan_seen {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(orphan_seen, "cgroup census did not retain orphaned member");
        assert!(!members.is_empty());
    }

    #[test]
    fn linux_fixture_acquisition_captures_preacquisition_setsid_orphan() {
        // The nested session exits before acquisition while its sleep child
        // remains alive.  The child therefore has a different SID and is
        // already reparented to the subreaper when the first census runs.
        let fixture =
            Fixture::create_with_script("setsid sh -c 'setsid sleep 30 & exit 0' & sleep 30");
        let root_identity = ProcessIdentity {
            pid: fixture.process.id() as libc::pid_t,
            starttime: process_starttime(fixture.process.id() as libc::pid_t)
                .unwrap()
                .unwrap(),
        };
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let root_session = process_stat(root_identity.pid).unwrap().unwrap().session;
        let orphan_deadline = Instant::now() + Duration::from_secs(1);
        while Instant::now() < orphan_deadline {
            let orphan_ready =
                read_children(root_identity)
                    .ok()
                    .into_iter()
                    .flatten()
                    .any(|member| {
                        process_stat(member.pid).ok().flatten().is_some_and(|stat| {
                            stat.session != root_session && stat.parent == unsafe { libc::getpid() }
                        })
                    });
            if orphan_ready {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        assert!(
            backend
                .initial_census
                .as_ref()
                .unwrap()
                .members
                .iter()
                .any(|member| {
                    process_stat(member.pid).ok().flatten().is_some_and(|stat| {
                        stat.session != root_session && stat.parent == unsafe { libc::getpid() }
                    })
                }),
            "pre-acquisition setsid orphan was not retained"
        );
        drop(acquired);
    }

    #[test]
    fn linux_fixture_census_includes_direct_setsid_descendant() {
        // A descendant may create a new session while remaining in the
        // runtime cgroup.  Descendant proof, not the root SID alone, owns the
        // kill set for this still-attached process.
        let fixture = Fixture::create_with_script("setsid sleep 30 & sleep 30");
        let root = ProcessIdentity {
            pid: fixture.process.id() as libc::pid_t,
            starttime: process_starttime(fixture.process.id() as libc::pid_t)
                .unwrap()
                .unwrap(),
        };
        let deadline = Instant::now() + Duration::from_secs(1);
        let mut found_new_session = false;
        while Instant::now() < deadline {
            let members = read_children(root).unwrap();
            found_new_session = members.iter().any(|member| {
                process_stat(member.pid)
                    .ok()
                    .flatten()
                    .is_some_and(|stat| stat.session != root.pid)
            });
            if found_new_session {
                break;
            }
            thread::sleep(Duration::from_millis(5));
        }
        assert!(
            found_new_session,
            "setsid descendant was omitted from census"
        );
    }

    #[test]
    fn linux_fixture_wait_does_not_drop_orphaned_setsid_member() {
        // Keep a new-session child alive while its parent exits.  The child
        // is no longer reachable through the root's children file, so only
        // the retained (pid,starttime) capability in cgroup.procs can keep
        // wait_until_empty from falsely declaring quiescence.
        let mut fixture = Fixture::create_with_script("setsid sleep 30 & wait");
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        assert!(backend
            .member_identities
            .iter()
            .any(|identity| process_stat(identity.pid)
                .ok()
                .flatten()
                .is_some_and(|stat| stat.session != backend.root_identity.unwrap().pid)));
        drop(acquired);
        fixture.process.kill().unwrap();
        fixture.process.wait().unwrap();
        let error = backend
            .wait_until_empty(Duration::from_millis(40))
            .unwrap_err();
        assert!(matches!(error, ControllerError::MembershipChanged));
        for fd in &backend.member_pidfds {
            let _ = pidfd_force_kill(fd.as_raw_fd());
        }
    }

    #[test]
    fn linux_fixture_stop_barrier_failure_resumes_root() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        backend.fail_after_root_stop = true;
        let pending = bound.request_shutdown(initial, &mut backend).unwrap_err();
        assert_eq!(
            pending.error(),
            &ControllerError::SystemCall("injected barrier failure")
        );
        assert!(!pending
            .obligations()
            .contains(&RecoveryObligation::StoppedProcess));
        let root = backend.root_identity.unwrap();
        assert_ne!(process_stat(root.pid).unwrap().unwrap().state, b'T');
        assert!(!backend.stop_barrier_active);
    }

    #[test]
    fn linux_fixture_stop_barrier_resume_failure_preserves_both_obligations() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        backend.fail_after_root_stop = true;
        backend.fail_resume = true;
        let pending = bound.request_shutdown(initial, &mut backend).unwrap_err();
        assert_eq!(
            pending.error(),
            &ControllerError::SystemCall("injected barrier failure")
        );
        assert_eq!(
            pending.obligations(),
            &[
                RecoveryObligation::SignalEvidence,
                RecoveryObligation::StoppedProcess
            ]
        );
        assert!(backend.stop_barrier_active);
        backend.fail_resume = false;
        backend.abort_after_error();
        assert!(!backend.stop_barrier_active);
    }

    #[test]
    fn linux_fixture_shutdown_budget_failure_resumes_barrier() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let mut request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        // Acquisition, membership binding, and the first member signal consume
        // three events.  The root event is intentionally rejected after the
        // member has been killed, exercising controller -> backend unwind.
        request.budget.max_events = 3;
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let pending = bound.request_shutdown(initial, &mut backend).unwrap_err();
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::BudgetExceeded));
        let root = backend.root_identity.unwrap();
        assert_ne!(process_stat(root.pid).unwrap().unwrap().state, b'T');
        assert!(!backend.stop_barrier_active);
    }

    #[test]
    fn linux_fixture_rejects_init_pid_swap_after_acquisition() {
        let fixture = Fixture::create();
        let pid_path = fixture
            .root
            .join(std::str::from_utf8(INIT_PID_NAME).unwrap());
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        std::fs::remove_file(&pid_path).unwrap();
        std::fs::write(&pid_path, format!("{}\n", fixture.process.id())).unwrap();
        set_permissions(&pid_path, Permissions::from_mode(0o600)).unwrap();
        let error = backend.membership_snapshot().unwrap_err();
        assert!(matches!(
            error,
            ControllerError::IdentityMismatch(RecoveryObligation::MembershipChanged)
        ));
        drop(acquired);
    }

    #[test]
    fn linux_fixture_rejects_late_fork_between_acquisition_and_signal() {
        let fixture = Fixture::create_with_script("sh -c 'sleep 0.2; sleep 30' & wait");
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        thread::sleep(Duration::from_millis(300));
        let error = backend.membership_snapshot().unwrap_err();
        assert!(matches!(
            error,
            ControllerError::IdentityMismatch(RecoveryObligation::LateFork)
                | ControllerError::IdentityMismatch(RecoveryObligation::MembershipChanged)
        ));
        drop(acquired);
    }

    #[test]
    fn linux_fixture_rejects_oversized_children_file_instead_of_marking_complete() {
        let fixture = Fixture::create();
        let path = fixture.root.join("oversized-children");
        std::fs::write(&path, vec![b'1'; MAX_CHILDREN_BYTES + 1]).unwrap();
        let error = read_bounded_path(
            path.to_string_lossy().into_owned(),
            MAX_CHILDREN_BYTES,
            "children",
        )
        .unwrap_err();
        assert!(matches!(error, ControllerError::BudgetExceeded));
    }

    #[test]
    fn linux_fixture_rejects_split_lock_before_observation() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let _acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let lock_name = component(LOCK_NAME).unwrap();
        assert_eq!(
            unsafe {
                libc::unlinkat(
                    backend.anchor_guard.as_ref().unwrap().as_raw_fd(),
                    lock_name.as_ptr(),
                    0,
                )
            },
            0
        );
        let replacement = unsafe {
            libc::openat(
                backend.anchor_guard.as_ref().unwrap().as_raw_fd(),
                lock_name.as_ptr(),
                libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                0o600,
            )
        };
        assert!(replacement >= 0);
        unsafe { libc::close(replacement) };
        let error = backend.membership_snapshot().unwrap_err();
        assert!(matches!(
            error,
            ControllerError::IdentityMismatch(RecoveryObligation::QuiescenceRequired)
        ));
    }

    #[test]
    fn linux_fixture_rejects_marker_in_place_mutation_before_signal() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let _acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        std::fs::write(
            fixture.root.join(std::str::from_utf8(MARKER_NAME).unwrap()),
            b"privileged-eunion\n",
        )
        .unwrap();
        let error = backend.membership_snapshot().unwrap_err();
        assert!(matches!(
            error,
            ControllerError::IdentityMismatch(RecoveryObligation::MarkerIdentity)
        ));
    }

    #[test]
    fn linux_fixture_rejects_regular_file_runtime_endpoint() {
        let fixture = Fixture::create();
        let endpoint = fixture.root.join("var/run/launchd.sock");
        std::fs::remove_file(&endpoint).unwrap();
        std::fs::write(&endpoint, b"not-a-socket").unwrap();
        set_permissions(&endpoint, Permissions::from_mode(0o600)).unwrap();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        assert!(prepared.acquire(anchor_cap, &mut backend).is_err());
    }

    #[test]
    fn linux_fixture_rejects_pidfd_ebadf_instead_of_calling_it_gone() {
        let fixture = Fixture::create();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let _acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let invalid = backend.root_pidfd.take().unwrap();
        let root_fd = invalid.as_raw_fd();
        assert_eq!(unsafe { libc::close(root_fd) }, 0);
        std::mem::forget(invalid);
        // Keep an intentionally invalid descriptor in the backend long enough
        // to prove EBADF is not treated as process death.  Forget it after
        // the assertion so OwnedFd never closes an already-closed fd.
        backend.root_pidfd = Some(unsafe { OwnedFd::from_raw_fd(root_fd) });
        let error = backend
            .wait_until_empty(Duration::from_millis(10))
            .unwrap_err();
        assert!(matches!(error, ControllerError::SystemCall(_)));
        std::mem::forget(backend.root_pidfd.take().unwrap());
    }

    #[test]
    fn linux_fixture_refuses_replacement_in_final_lease_window() {
        let fixture = Fixture::create();
        let replacement = fixture.root.join("var/run/shellspawn.sock");
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let acquired = prepared.acquire(anchor_cap, &mut backend).unwrap();
        let initial = backend.membership_snapshot().unwrap();
        let bound = acquired.bind_membership(initial.clone()).unwrap();
        let requested = bound.request_shutdown(initial, &mut backend).unwrap();
        backend.wait_until_empty(Duration::from_secs(2)).unwrap();
        let drained = requested
            .drain(MembershipSnapshot::empty(
                MembershipSource::RustProcTaskChildren,
            ))
            .unwrap();
        let quiescent = drained.establish_quiescence(&mut backend).unwrap();
        backend.replace_shellspawn_after_verify = true;
        backend.invalidate_lease_after_verify = true;
        let pending = quiescent.cleanup(&mut backend).unwrap_err();
        assert!(pending
            .obligations()
            .contains(&RecoveryObligation::QuiescenceRequired));
        assert_eq!(std::fs::read(&replacement).unwrap(), b"replacement");
    }

    #[test]
    fn linux_fixture_rejects_marker_symlink_and_wrong_mode() {
        {
            let fixture = Fixture::create();
            let marker = fixture.root.join(std::str::from_utf8(MARKER_NAME).unwrap());
            std::fs::remove_file(&marker).unwrap();
            std::os::unix::fs::symlink(
                fixture
                    .root
                    .join(std::str::from_utf8(LAUNCHER_NAME).unwrap()),
                &marker,
            )
            .unwrap();
            let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
            let request = fixture_request(
                anchor.as_raw_fd(),
                LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
            );
            let prepared = Controller::prepare(request).unwrap();
            let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
            let mut backend = LinuxBackend::default();
            assert!(prepared.acquire(anchor_cap, &mut backend).is_err());
        }

        let fixture = Fixture::create();
        let marker = fixture.root.join(std::str::from_utf8(MARKER_NAME).unwrap());
        set_permissions(&marker, Permissions::from_mode(0o644)).unwrap();
        let anchor: OwnedFd = File::open(&fixture.root).unwrap().into();
        let request = fixture_request(
            anchor.as_raw_fd(),
            LinuxBackend::launcher_digest(&fixture.launcher_bytes()),
        );
        let prepared = Controller::prepare(request).unwrap();
        let anchor_cap = AnchorCapabilities::from_inherited(anchor, None).unwrap();
        let mut backend = LinuxBackend::default();
        let error = prepared.acquire(anchor_cap, &mut backend).unwrap_err();
        assert!(matches!(
            error,
            ControllerError::IdentityMismatch(RecoveryObligation::MarkerIdentity)
        ));
    }
}
