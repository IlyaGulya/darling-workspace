//! Bounded, read-only process census for a retained owned-scratch root.

use serde::{Deserialize, Serialize};
use std::fs;
use std::io;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStringExt;
use std::os::unix::fs::{FileTypeExt, MetadataExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

pub const PROTOCOL_VERSION: u32 = 1;
pub const MAX_PROCESS_LIMIT: usize = 262_144;
pub const MAX_FD_LIMIT: usize = 65_536;
pub const MAX_TIME_LIMIT_MS: u64 = 60_000;
pub const MAX_OUTPUT_LIMIT_BYTES: usize = 1_048_576;
pub const MAX_IGNORED_DESCRIPTORS: usize = 1_024;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProcessIdentity {
    pub pid: u32,
    pub tid: u32,
    pub starttime: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ReferenceSource {
    Cwd,
    Root,
    Exe,
    Fd,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum CensusOperation {
    Cwd,
    Root,
    Exe,
    Fd,
    Stat,
    StatRevalidate,
    FdCensus,
    ProcessMetadata,
    TaskCensus,
    TaskEntry,
    ProcCensus,
    ProcEntry,
    ParseStat,
    ParseFd,
    ProcessIdentityChanged,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(tag = "kind", rename_all = "snake_case", deny_unknown_fields)]
pub enum CensusOutcome {
    Reference {
        identity: ProcessIdentity,
        source: ReferenceSource,
        descriptor: Option<u32>,
    },
    Unreadable {
        pid: u32,
        tid: Option<u32>,
        operation: CensusOperation,
        errno: i32,
    },
    FdOverflow {
        identity: ProcessIdentity,
        observed: usize,
        limit: usize,
    },
    Ambiguous {
        pid: u32,
        tid: Option<u32>,
        operation: CensusOperation,
        errno: Option<i32>,
    },
    BudgetExceeded {
        budget: BudgetKind,
    },
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum BudgetKind {
    Processes,
    Time,
    Output,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct IgnoredDescriptor {
    pub pid: u32,
    pub fd: u32,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CensusRequest {
    pub protocol_version: u32,
    pub root_fd: i32,
    pub process_limit: usize,
    pub fd_limit: usize,
    pub time_limit_ms: u64,
    pub output_limit_bytes: usize,
    #[serde(default)]
    pub ignored_descriptors: Vec<IgnoredDescriptor>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CensusResponse {
    pub protocol_version: u32,
    pub outcomes: Vec<CensusOutcome>,
}

#[derive(Debug)]
pub enum CensusError {
    UnsupportedProtocol,
    InvalidBudget,
    InvalidDescriptor(io::Error),
    InvalidRoot(io::Error),
}

impl std::fmt::Display for CensusError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::UnsupportedProtocol => write!(formatter, "unsupported scratch census protocol"),
            Self::InvalidBudget => write!(formatter, "invalid scratch census budget"),
            Self::InvalidDescriptor(error) => {
                write!(formatter, "invalid inherited scratch descriptor: {error}")
            }
            Self::InvalidRoot(error) => write!(formatter, "invalid retained scratch root: {error}"),
        }
    }
}

pub fn duplicate_inherited_root(raw_fd: RawFd) -> Result<OwnedFd, CensusError> {
    if raw_fd <= libc::STDERR_FILENO {
        return Err(CensusError::InvalidDescriptor(
            io::Error::from_raw_os_error(libc::EBADF),
        ));
    }
    // SAFETY: fcntl does not borrow Rust memory. On success F_DUPFD_CLOEXEC
    // returns a new descriptor-table entry owned exclusively by this call.
    let duplicated = unsafe { libc::fcntl(raw_fd, libc::F_DUPFD_CLOEXEC, 3) };
    if duplicated < 0 {
        return Err(CensusError::InvalidDescriptor(io::Error::last_os_error()));
    }
    // SAFETY: `duplicated` is the fresh, successful F_DUPFD_CLOEXEC result and
    // has not been wrapped or transferred elsewhere.
    Ok(unsafe { OwnedFd::from_raw_fd(duplicated) })
}

impl std::error::Error for CensusError {}

fn errno(error: &io::Error) -> i32 {
    error.raw_os_error().unwrap_or(libc::EIO)
}

fn permission_error(error: &io::Error) -> bool {
    matches!(error.raw_os_error(), Some(libc::EACCES | libc::EPERM))
}

fn effective_uid() -> u32 {
    // SAFETY: geteuid has no arguments, cannot invalidate memory, and returns
    // the effective UID value for the calling process.
    unsafe { libc::geteuid() }
}

fn strip_deleted(path: &Path) -> PathBuf {
    let bytes = path.as_os_str().as_encoded_bytes();
    const DELETED: &[u8] = b" (deleted)";
    if let Some(prefix) = bytes.strip_suffix(DELETED) {
        PathBuf::from(std::ffi::OsString::from_vec(prefix.to_vec()))
    } else {
        path.to_path_buf()
    }
}

fn inside(path: &Path, root: &Path) -> bool {
    strip_deleted(path).starts_with(root)
}

fn parse_starttime(payload: &str) -> Option<u64> {
    let closing = payload.rfind(')')?;
    payload
        .get(closing + 1..)?
        .split_whitespace()
        .nth(19)?
        .parse()
        .ok()
}

fn revalidated_identity_outcome(
    identity: &ProcessIdentity,
    payload: &str,
) -> Option<CensusOutcome> {
    (parse_starttime(payload) != Some(identity.starttime)).then_some(CensusOutcome::Ambiguous {
        pid: identity.pid,
        tid: Some(identity.tid),
        operation: CensusOperation::ProcessIdentityChanged,
        errno: None,
    })
}

struct Scanner<'a> {
    request: &'a CensusRequest,
    root: PathBuf,
    proc_root: &'a Path,
    uid: u32,
    owned_root_fd: RawFd,
    started: Instant,
    processes: usize,
    outcomes: Vec<CensusOutcome>,
    output_capacity: usize,
    terminal: bool,
}

impl Scanner<'_> {
    fn push(&mut self, outcome: CensusOutcome) {
        if self.terminal {
            return;
        }
        if self.outcomes.len() >= self.output_capacity {
            self.outcomes.clear();
            self.outcomes.push(CensusOutcome::BudgetExceeded {
                budget: BudgetKind::Output,
            });
            self.terminal = true;
            return;
        }
        self.outcomes.push(outcome);
    }

    fn check_time(&mut self) -> bool {
        if self.started.elapsed() <= Duration::from_millis(self.request.time_limit_ms) {
            true
        } else {
            self.push(CensusOutcome::BudgetExceeded {
                budget: BudgetKind::Time,
            });
            self.terminal = true;
            false
        }
    }

    fn ambiguous(
        &mut self,
        pid: u32,
        tid: Option<u32>,
        operation: CensusOperation,
        error: &io::Error,
    ) {
        self.push(CensusOutcome::Ambiguous {
            pid,
            tid,
            operation,
            errno: error.raw_os_error(),
        });
    }

    fn unreadable(
        &mut self,
        pid: u32,
        tid: Option<u32>,
        operation: CensusOperation,
        error: &io::Error,
    ) {
        self.push(CensusOutcome::Unreadable {
            pid,
            tid,
            operation,
            errno: errno(error),
        });
    }

    fn scan_link(
        &mut self,
        task: &Path,
        identity: &ProcessIdentity,
        name: &str,
        source: ReferenceSource,
        descriptor: Option<u32>,
    ) {
        let operation = match source {
            ReferenceSource::Cwd => CensusOperation::Cwd,
            ReferenceSource::Root => CensusOperation::Root,
            ReferenceSource::Exe => CensusOperation::Exe,
            ReferenceSource::Fd => CensusOperation::Fd,
        };
        match fs::read_link(task.join(name)) {
            Ok(target) if inside(&target, &self.root) => self.push(CensusOutcome::Reference {
                identity: identity.clone(),
                source,
                descriptor,
            }),
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) if permission_error(&error) => {
                self.unreadable(identity.pid, Some(identity.tid), operation, &error)
            }
            Err(error) => self.ambiguous(identity.pid, Some(identity.tid), operation, &error),
        }
    }

    fn scan_task(&mut self, pid: u32, task: &Path, tid: u32) {
        if !self.check_time() || self.terminal {
            return;
        }
        let stat_path = task.join("stat");
        let before = match fs::read_to_string(&stat_path) {
            Ok(payload) => payload,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return,
            Err(error) if permission_error(&error) => {
                self.unreadable(pid, Some(tid), CensusOperation::Stat, &error);
                return;
            }
            Err(error) => {
                self.ambiguous(pid, Some(tid), CensusOperation::Stat, &error);
                return;
            }
        };
        let Some(starttime) = parse_starttime(&before) else {
            self.push(CensusOutcome::Ambiguous {
                pid,
                tid: Some(tid),
                operation: CensusOperation::ParseStat,
                errno: None,
            });
            return;
        };
        let identity = ProcessIdentity {
            pid,
            tid,
            starttime,
        };
        self.scan_link(task, &identity, "cwd", ReferenceSource::Cwd, None);
        self.scan_link(task, &identity, "root", ReferenceSource::Root, None);
        self.scan_link(task, &identity, "exe", ReferenceSource::Exe, None);

        let descriptors = match fs::read_dir(task.join("fd")) {
            Ok(entries) => entries,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return,
            Err(error) if permission_error(&error) => {
                self.unreadable(pid, Some(tid), CensusOperation::FdCensus, &error);
                return;
            }
            Err(error) => {
                self.ambiguous(pid, Some(tid), CensusOperation::FdCensus, &error);
                return;
            }
        };
        let mut observed = 0usize;
        for descriptor in descriptors {
            if !self.check_time() || self.terminal {
                return;
            }
            let descriptor = match descriptor {
                Ok(descriptor) => descriptor,
                Err(error) if permission_error(&error) => {
                    self.unreadable(pid, Some(tid), CensusOperation::FdCensus, &error);
                    return;
                }
                Err(error) => {
                    self.ambiguous(pid, Some(tid), CensusOperation::FdCensus, &error);
                    return;
                }
            };
            observed += 1;
            if observed > self.request.fd_limit {
                self.push(CensusOutcome::FdOverflow {
                    identity: identity.clone(),
                    observed,
                    limit: self.request.fd_limit,
                });
                return;
            }
            let Some(fd) = descriptor
                .file_name()
                .to_str()
                .and_then(|name| name.parse::<u32>().ok())
            else {
                self.push(CensusOutcome::Ambiguous {
                    pid,
                    tid: Some(tid),
                    operation: CensusOperation::ParseFd,
                    errno: None,
                });
                continue;
            };
            if self
                .request
                .ignored_descriptors
                .iter()
                .any(|ignored| ignored.pid == pid && ignored.fd == fd)
                || (pid == std::process::id()
                    && (fd as i32 == self.request.root_fd || fd as i32 == self.owned_root_fd))
            {
                continue;
            }
            let name = descriptor.file_name();
            self.scan_link(
                &task.join("fd"),
                &identity,
                name.to_string_lossy().as_ref(),
                ReferenceSource::Fd,
                Some(fd),
            );
        }
        match fs::read_to_string(stat_path) {
            Ok(after) => {
                if let Some(outcome) = revalidated_identity_outcome(&identity, &after) {
                    self.push(outcome);
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) if permission_error(&error) => {
                self.unreadable(pid, Some(tid), CensusOperation::StatRevalidate, &error)
            }
            Err(error) => self.ambiguous(pid, Some(tid), CensusOperation::StatRevalidate, &error),
        }
    }

    fn scan(mut self) -> CensusResponse {
        let processes = match fs::read_dir(self.proc_root) {
            Ok(entries) => entries,
            Err(error) => {
                self.ambiguous(0, None, CensusOperation::ProcCensus, &error);
                return CensusResponse {
                    protocol_version: PROTOCOL_VERSION,
                    outcomes: self.outcomes,
                };
            }
        };
        for process in processes {
            if !self.check_time() || self.terminal {
                break;
            }
            let process = match process {
                Ok(process) => process,
                Err(error) => {
                    self.ambiguous(0, None, CensusOperation::ProcEntry, &error);
                    continue;
                }
            };
            let Some(pid) = process
                .file_name()
                .to_str()
                .and_then(|name| name.parse::<u32>().ok())
            else {
                continue;
            };
            let metadata = match process.metadata() {
                Ok(metadata) => metadata,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) if permission_error(&error) => {
                    self.unreadable(pid, None, CensusOperation::ProcessMetadata, &error);
                    continue;
                }
                Err(error) => {
                    self.ambiguous(pid, None, CensusOperation::ProcessMetadata, &error);
                    continue;
                }
            };
            if metadata.uid() != self.uid {
                continue;
            }
            let tasks = match fs::read_dir(process.path().join("task")) {
                Ok(tasks) => tasks,
                Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
                Err(error) if permission_error(&error) => {
                    self.unreadable(pid, None, CensusOperation::TaskCensus, &error);
                    continue;
                }
                Err(error) => {
                    self.ambiguous(pid, None, CensusOperation::TaskCensus, &error);
                    continue;
                }
            };
            for task in tasks {
                let task = match task {
                    Ok(task) => task,
                    Err(error) => {
                        self.ambiguous(pid, None, CensusOperation::TaskEntry, &error);
                        continue;
                    }
                };
                let Some(tid) = task
                    .file_name()
                    .to_str()
                    .and_then(|name| name.parse::<u32>().ok())
                else {
                    continue;
                };
                self.processes += 1;
                if self.processes > self.request.process_limit {
                    self.push(CensusOutcome::BudgetExceeded {
                        budget: BudgetKind::Processes,
                    });
                    self.terminal = true;
                    break;
                }
                self.scan_task(pid, &task.path(), tid);
            }
        }
        CensusResponse {
            protocol_version: PROTOCOL_VERSION,
            outcomes: self.outcomes,
        }
    }
}

pub fn census(root_fd: OwnedFd, request: &CensusRequest) -> Result<CensusResponse, CensusError> {
    census_at(root_fd, request, Path::new("/proc"), effective_uid())
}

fn census_at(
    root_fd: OwnedFd,
    request: &CensusRequest,
    proc_root: &Path,
    uid: u32,
) -> Result<CensusResponse, CensusError> {
    if request.protocol_version != PROTOCOL_VERSION {
        return Err(CensusError::UnsupportedProtocol);
    }
    if request.process_limit == 0
        || request.process_limit > MAX_PROCESS_LIMIT
        || request.fd_limit == 0
        || request.fd_limit > MAX_FD_LIMIT
        || request.time_limit_ms == 0
        || request.time_limit_ms > MAX_TIME_LIMIT_MS
        || request.output_limit_bytes < 256
        || request.output_limit_bytes > MAX_OUTPUT_LIMIT_BYTES
        || request.ignored_descriptors.len() > MAX_IGNORED_DESCRIPTORS
    {
        return Err(CensusError::InvalidBudget);
    }
    let owned_root_fd = root_fd.as_raw_fd();
    let root_metadata =
        fs::metadata(format!("/proc/self/fd/{owned_root_fd}")).map_err(CensusError::InvalidRoot)?;
    if !root_metadata.file_type().is_dir()
        || root_metadata.file_type().is_symlink()
        || root_metadata.file_type().is_fifo()
        || root_metadata.file_type().is_socket()
    {
        return Err(CensusError::InvalidRoot(io::Error::from_raw_os_error(
            libc::ENOTDIR,
        )));
    }
    let root = fs::read_link(format!("/proc/self/fd/{owned_root_fd}"))
        .map_err(CensusError::InvalidRoot)?;
    let output_capacity = (request.output_limit_bytes / 192).max(1);
    Ok(Scanner {
        request,
        root: strip_deleted(&root),
        proc_root,
        uid,
        owned_root_fd,
        started: Instant::now(),
        processes: 0,
        outcomes: Vec::new(),
        output_capacity,
        terminal: false,
    }
    .scan())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs::File;
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::symlink;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn fixture() -> (PathBuf, PathBuf, OwnedFd) {
        let root = std::env::temp_dir().join(format!(
            "darling-scratch-census-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let scratch = root.join("scratch");
        let proc = root.join("proc");
        fs::create_dir_all(&scratch).unwrap();
        fs::create_dir_all(&proc).unwrap();
        let file = File::open(&scratch).unwrap();
        let fd = OwnedFd::from(file);
        (root, proc, fd)
    }

    fn request(fd: i32) -> CensusRequest {
        CensusRequest {
            protocol_version: PROTOCOL_VERSION,
            root_fd: fd,
            process_limit: 16,
            fd_limit: 16,
            time_limit_ms: 1_000,
            output_limit_bytes: 16_384,
            ignored_descriptors: Vec::new(),
        }
    }

    fn stat_payload(starttime: u64) -> String {
        format!("1 (fixture name) S 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 {starttime} 0 0")
    }

    #[test]
    fn reports_pid_tid_starttime_and_reference() {
        let (base, proc, root_fd) = fixture();
        let task = proc.join("41/task/43");
        fs::create_dir_all(task.join("fd")).unwrap();
        fs::write(task.join("stat"), stat_payload(9001)).unwrap();
        symlink(base.join("scratch"), task.join("cwd")).unwrap();
        symlink("/", task.join("root")).unwrap();
        symlink("/bin/sh", task.join("exe")).unwrap();
        let uid = effective_uid();
        let response = census_at(root_fd, &request(999), &proc, uid).unwrap();
        assert!(response.outcomes.iter().any(|outcome| matches!(
            outcome,
            CensusOutcome::Reference { identity, source: ReferenceSource::Cwd, .. }
                if identity.pid == 41 && identity.tid == 43 && identity.starttime == 9001
        )));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn malformed_identity_and_budgets_are_typed() {
        let (base, proc, root_fd) = fixture();
        let task = proc.join("41/task/41");
        fs::create_dir_all(task.join("fd")).unwrap();
        fs::write(task.join("stat"), "malformed").unwrap();
        let uid = effective_uid();
        let response = census_at(root_fd, &request(999), &proc, uid).unwrap();
        assert!(matches!(
            response.outcomes.as_slice(),
            [CensusOutcome::Ambiguous {
                operation: CensusOperation::ParseStat,
                ..
            }]
        ));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn tid_and_fd_budgets_are_terminal_and_typed() {
        let (base, proc, root_fd) = fixture();
        for tid in [41, 42] {
            let task = proc.join(format!("41/task/{tid}"));
            fs::create_dir_all(task.join("fd")).unwrap();
            fs::write(task.join("stat"), stat_payload(tid as u64)).unwrap();
            symlink("/", task.join("cwd")).unwrap();
            symlink("/", task.join("root")).unwrap();
            symlink("/bin/sh", task.join("exe")).unwrap();
        }
        let uid = effective_uid();
        let mut limited = request(999);
        limited.process_limit = 1;
        let response = census_at(root_fd, &limited, &proc, uid).unwrap();
        assert!(response.outcomes.iter().any(|outcome| matches!(
            outcome,
            CensusOutcome::BudgetExceeded {
                budget: BudgetKind::Processes
            }
        )));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn scanner_reports_changed_starttime_as_ambiguous() {
        let (base, proc, root_fd) = fixture();
        let task = proc.join("41/task/43");
        fs::create_dir_all(task.join("fd")).unwrap();
        fs::write(task.join("stat"), stat_payload(10)).unwrap();
        symlink(base.join("scratch"), task.join("cwd")).unwrap();
        symlink("/", task.join("root")).unwrap();
        symlink("/bin/sh", task.join("exe")).unwrap();

        let identity = ProcessIdentity {
            pid: 41,
            tid: 43,
            starttime: parse_starttime(&fs::read_to_string(task.join("stat")).unwrap()).unwrap(),
        };
        fs::write(task.join("stat"), stat_payload(11)).unwrap();
        let after = fs::read_to_string(task.join("stat")).unwrap();
        assert!(matches!(
            revalidated_identity_outcome(&identity, &after),
            Some(CensusOutcome::Ambiguous {
                operation: CensusOperation::ProcessIdentityChanged,
                ..
            })
        ));
        drop(root_fd);
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn request_protocol_rejects_extra_fields() {
        let payload = r#"{"protocol_version":1,"root_fd":3,"process_limit":1,"fd_limit":1,"time_limit_ms":1,"output_limit_bytes":256,"ignored_descriptors":[],"extra":true}"#;
        assert!(serde_json::from_str::<CensusRequest>(payload).is_err());

        let (base, proc, root_fd) = fixture();
        let mut unsupported = request(root_fd.as_raw_fd());
        unsupported.protocol_version += 1;
        assert!(matches!(
            census_at(root_fd, &unsupported, &proc, effective_uid()),
            Err(CensusError::UnsupportedProtocol)
        ));
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn request_budgets_have_hard_upper_bounds() {
        for mutation in 0..5 {
            let mut oversized = request(3);
            match mutation {
                0 => oversized.process_limit = MAX_PROCESS_LIMIT + 1,
                1 => oversized.fd_limit = MAX_FD_LIMIT + 1,
                2 => oversized.time_limit_ms = MAX_TIME_LIMIT_MS + 1,
                3 => oversized.output_limit_bytes = MAX_OUTPUT_LIMIT_BYTES + 1,
                4 => {
                    oversized.ignored_descriptors = (0..=MAX_IGNORED_DESCRIPTORS)
                        .map(|fd| IgnoredDescriptor {
                            pid: 1,
                            fd: fd as u32,
                        })
                        .collect()
                }
                _ => unreachable!(),
            }
            let (base, proc, root_fd) = fixture();
            assert!(matches!(
                census_at(root_fd, &oversized, &proc, effective_uid()),
                Err(CensusError::InvalidBudget)
            ));
            fs::remove_dir_all(base).unwrap();
        }
    }

    #[test]
    fn inherited_descriptor_is_validated_and_duplicated_cloexec() {
        for raw in [
            libc::STDIN_FILENO,
            libc::STDOUT_FILENO,
            libc::STDERR_FILENO,
            999_999,
        ] {
            assert!(matches!(
                duplicate_inherited_root(raw),
                Err(CensusError::InvalidDescriptor(_))
            ));
        }
        let (base, _proc, root_fd) = fixture();
        let original = root_fd.as_raw_fd();
        let duplicate = duplicate_inherited_root(original).unwrap();
        assert_ne!(duplicate.as_raw_fd(), original);
        // SAFETY: F_GETFD only inspects the live descriptor owned above.
        let flags = unsafe { libc::fcntl(duplicate.as_raw_fd(), libc::F_GETFD) };
        assert_ne!(flags & libc::FD_CLOEXEC, 0);
        assert!(fs::metadata(format!("/proc/self/fd/{original}")).is_ok());
        fs::remove_dir_all(base).unwrap();
    }

    #[test]
    fn output_and_time_budgets_are_terminal() {
        let (base, proc, root_fd) = fixture();
        for pid in [41, 42] {
            let task = proc.join(format!("{pid}/task/{pid}"));
            fs::create_dir_all(task.join("fd")).unwrap();
            fs::write(task.join("stat"), "malformed").unwrap();
        }
        let uid = effective_uid();
        let mut limited = request(999);
        limited.output_limit_bytes = 256;
        let response = census_at(root_fd, &limited, &proc, uid).unwrap();
        assert_eq!(
            response.outcomes,
            vec![CensusOutcome::BudgetExceeded {
                budget: BudgetKind::Output
            }]
        );

        let (other_base, other_proc, other_fd) = fixture();
        let request = request(other_fd.as_raw_fd());
        let mut scanner = Scanner {
            request: &request,
            root: PathBuf::from("/nonexistent"),
            proc_root: &other_proc,
            uid,
            owned_root_fd: other_fd.as_raw_fd(),
            started: Instant::now() - Duration::from_secs(2),
            processes: 0,
            outcomes: Vec::new(),
            output_capacity: 16,
            terminal: false,
        };
        assert!(!scanner.check_time());
        assert!(matches!(
            scanner.outcomes.as_slice(),
            [CensusOutcome::BudgetExceeded {
                budget: BudgetKind::Time
            }]
        ));
        fs::remove_dir_all(base).unwrap();
        fs::remove_dir_all(other_base).unwrap();
    }
}
