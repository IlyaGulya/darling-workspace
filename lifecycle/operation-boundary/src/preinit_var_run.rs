//! Crash-recoverable, generation-owned lifecycle for the public `var/run`.
use serde::{Deserialize, Serialize};
use std::{
    ffi::{CStr, CString},
    io,
    mem::MaybeUninit,
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
    time::{Duration, Instant},
};

const STATE: &CStr = c".darling-var-run-state-v1";
const SNAPSHOT_A: &CStr = c".darling-var-run-state-v1.snapshot-a";
const SNAPSHOT_B: &CStr = c".darling-var-run-state-v1.snapshot-b";
const VAR: &CStr = c"var";
const RUN: &CStr = c"run";
const MAX_WAL: usize = 262144;
const MAX_SNAPSHOT: usize = 65536;
const MAX_ENTRIES: usize = 4096;
const MAX_DEPTH: usize = 32;
const MAX_FORENSIC: usize = 16;
const MAX_PROCESSES: usize = 32768;
const MAX_FDS: usize = 65536;

#[cfg(test)]
thread_local! { static FAULT: std::cell::Cell<u8> = const { std::cell::Cell::new(0) }; }
#[cfg(test)]
pub(crate) fn inject_fault(point: u8) {
    FAULT.with(|fault| fault.set(point));
}
#[cfg(test)]
fn checkpoint(point: u8) -> Result<(), VarRunError> {
    FAULT.with(|fault| {
        if fault.get() == point {
            fault.set(0);
            Err(VarRunError::recovery("injected var/run interruption"))
        } else {
            Ok(())
        }
    })
}
#[cfg(not(test))]
fn checkpoint(_: u8) -> Result<(), VarRunError> {
    Ok(())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
struct Id {
    dev: u64,
    ino: u64,
    uid: u32,
    gid: u32,
    mode: u32,
    nlink: u64,
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Phase {
    Active,
    MovePending,
    Moved,
    CreatePending,
    Created,
    PublishPending,
    Published,
    CollectPending,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct State {
    schema_version: u32,
    prefix: Id,
    var_parent: Id,
    generation: u64,
    phase: Phase,
    public: Option<Id>,
    old_name: Option<String>,
    old_id: Option<Id>,
    collect: bool,
    new_name: Option<String>,
    new_id: Option<Id>,
    forensic: Vec<ForensicRecord>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ForensicRecord {
    name: String,
    identity: Id,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct Snapshot {
    schema_version: u32,
    sequence: u64,
    state: State,
}

#[derive(Debug)]
pub struct VarRunError {
    op: &'static str,
    source: Option<io::Error>,
    recovery: bool,
}
impl VarRunError {
    fn protocol(op: &'static str) -> Self {
        Self {
            op,
            source: None,
            recovery: false,
        }
    }
    fn recovery(op: &'static str) -> Self {
        Self {
            op,
            source: None,
            recovery: true,
        }
    }
    fn io(op: &'static str) -> Self {
        Self {
            op,
            source: Some(io::Error::last_os_error()),
            recovery: false,
        }
    }
    fn owning(mut self) -> Self {
        self.recovery = true;
        self
    }
    pub fn recovery_required(&self) -> bool {
        self.recovery
    }
}
impl std::fmt::Display for VarRunError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if let Some(e) = &self.source {
            write!(f, "{}: {e}", self.op)
        } else {
            f.write_str(self.op)
        }
    }
}
impl std::error::Error for VarRunError {}
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct VarRunOutcome {
    pub generation: u64,
    pub identity: (u64, u64),
    pub forensic_quarantine: Option<String>,
}

fn id(fd: RawFd) -> Result<Id, VarRunError> {
    let mut s = MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, s.as_mut_ptr()) } != 0 {
        return Err(VarRunError::io("fstat capability"));
    }
    let s = unsafe { s.assume_init() };
    Ok(Id {
        dev: s.st_dev,
        ino: s.st_ino,
        uid: s.st_uid,
        gid: s.st_gid,
        mode: s.st_mode,
        nlink: s.st_nlink,
    })
}
fn named(fd: RawFd, n: &CStr) -> Result<Option<Id>, VarRunError> {
    let mut s = MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstatat(fd, n.as_ptr(), s.as_mut_ptr(), libc::AT_SYMLINK_NOFOLLOW) } == 0 {
        let s = unsafe { s.assume_init() };
        return Ok(Some(Id {
            dev: s.st_dev,
            ino: s.st_ino,
            uid: s.st_uid,
            gid: s.st_gid,
            mode: s.st_mode,
            nlink: s.st_nlink,
        }));
    }
    let e = io::Error::last_os_error();
    if e.raw_os_error() == Some(libc::ENOENT) {
        Ok(None)
    } else {
        Err(VarRunError {
            op: "stat name",
            source: Some(e),
            recovery: false,
        })
    }
}
fn open_dir(fd: RawFd, n: &CStr) -> Result<OwnedFd, VarRunError> {
    let x = unsafe {
        libc::openat(
            fd,
            n.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if x < 0 {
        Err(VarRunError::io("open directory"))
    } else {
        Ok(unsafe { OwnedFd::from_raw_fd(x) })
    }
}
fn valid_dir(x: Id, owner: Id) -> bool {
    x.mode & libc::S_IFMT == libc::S_IFDIR
        && x.nlink >= 2
        && x.uid == owner.uid
        && x.gid == owner.gid
}
fn same_authority(a: Id, b: Id) -> bool {
    a.dev == b.dev && a.ino == b.ino && a.uid == b.uid && a.gid == b.gid && a.mode == b.mode
}
fn same_object(a: Id, b: Id) -> bool {
    same_authority(a, b)
}
fn validate_forensic(var: RawFd, state: &State) -> Result<(), VarRunError> {
    for record in &state.forensic {
        let name = CString::new(record.name.as_bytes())
            .map_err(|_| VarRunError::recovery("invalid forensic name"))?;
        match named(var, &name).map_err(VarRunError::owning)? {
            Some(observed) if same_authority(observed, record.identity) => {}
            None => {
                let pending_source = match state.phase {
                    Phase::MovePending
                        if state.old_name.as_deref() == Some(record.name.as_str()) =>
                    {
                        named(var, RUN)?.is_some_and(|value| {
                            same_authority(value, record.identity)
                                && state.old_id == Some(record.identity)
                        })
                    }
                    Phase::CreatePending => state
                        .new_name
                        .as_ref()
                        .and_then(|source| CString::new(source.as_bytes()).ok())
                        .and_then(|source| named(var, &source).ok().flatten())
                        .is_some_and(|value| same_authority(value, record.identity)),
                    _ => false,
                };
                if !pending_source {
                    return Err(VarRunError::recovery("forensic object missing"));
                }
            }
            Some(_) => return Err(VarRunError::recovery("forensic object replacement")),
        }
    }
    Ok(())
}
fn revalidate(prefix: RawFd, prefix_id: Id, var: RawFd, var_id: Id) -> Result<(), VarRunError> {
    if !same_authority(id(prefix)?, prefix_id)
        || !same_authority(id(var)?, var_id)
        || !named(prefix, VAR)?.is_some_and(|named| same_authority(named, var_id))
    {
        Err(VarRunError::recovery("prefix or var parent replacement"))
    } else {
        Ok(())
    }
}
fn sync(fd: RawFd) -> Result<(), VarRunError> {
    if unsafe { libc::fsync(fd) } == 0 {
        Ok(())
    } else {
        Err(VarRunError::io("fsync directory"))
    }
}
fn rename_nr(a: RawFd, x: &CStr, b: RawFd, y: &CStr) -> Result<(), VarRunError> {
    if unsafe {
        libc::syscall(
            libc::SYS_renameat2,
            a,
            x.as_ptr(),
            b,
            y.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    } == 0
    {
        Ok(())
    } else {
        Err(VarRunError::io("renameat2 transition"))
    }
}
fn cname(x: &Option<String>) -> Result<CString, VarRunError> {
    CString::new(
        x.as_deref()
            .ok_or_else(|| VarRunError::recovery("missing transition name"))?,
    )
    .map_err(|_| VarRunError::recovery("invalid transition name"))
}
fn transition(p: &str, g: u64, x: Id) -> CString {
    CString::new(format!(".{p}-{g:x}-{:x}-{:x}", x.dev, x.ino)).expect("static name")
}
fn fresh(g: u64) -> Result<CString, VarRunError> {
    let mut r = 0u64;
    if unsafe { libc::getrandom((&raw mut r).cast(), 8, 0) } != 8 {
        return Err(VarRunError::io("transaction nonce"));
    }
    Ok(CString::new(format!(".run.new-{g:x}-{r:x}")).expect("numeric name"))
}
struct DirStream(*mut libc::DIR);
impl Drop for DirStream {
    fn drop(&mut self) {
        if !self.0.is_null() {
            unsafe { libc::closedir(self.0) };
        }
    }
}

struct Wal {
    fd: OwnedFd,
    len: usize,
    parent: RawFd,
    identity: Id,
    snapshots: [(OwnedFd, Id); 2],
    snapshot_sequence: u64,
    active_snapshot: Option<usize>,
    last: Option<State>,
}
impl Wal {
    fn open(prefix: RawFd, _var: RawFd, owner: Id) -> Result<(Self, Option<State>), VarRunError> {
        let (fd, i) = open_state_file(prefix, STATE, owner, true, [40, 41, 42, 43])?;
        let (snapshot_a, snapshot_a_id) =
            open_state_file(prefix, SNAPSHOT_A, owner, false, [44, 45, 46, 47])?;
        let (snapshot_b, snapshot_b_id) =
            open_state_file(prefix, SNAPSHOT_B, owner, false, [48, 49, 50, 51])?;
        // Always persist the retained directory after validating every slot.
        // This also completes a prior crash that left a created name visible
        // without proving that its directory entry reached stable storage.
        let sync_parent = open_dir(prefix, c".")?;
        if !same_authority(id(sync_parent.as_raw_fd())?, owner) {
            return Err(VarRunError::protocol("state directory capability mismatch"));
        }
        checkpoint(52)?;
        if unsafe { libc::fsync(sync_parent.as_raw_fd()) } != 0 {
            return Err(VarRunError::io("fsync state directory").owning());
        }
        checkpoint(53)?;
        let snapshots: [(OwnedFd, Id); 2] =
            vec![(snapshot_a, snapshot_a_id), (snapshot_b, snapshot_b_id)]
                .try_into()
                .map_err(|_| VarRunError::protocol("snapshot slot count"))?;
        let mut snapshot_sequence = 0;
        let mut active_snapshot = None;
        let mut last = None;
        for (slot, (snapshot_fd, _)) in snapshots.iter().enumerate() {
            if let Some(snapshot) = read_snapshot(snapshot_fd.as_raw_fd())? {
                if snapshot.schema_version != 1 {
                    return Err(VarRunError::protocol("snapshot schema"));
                }
                if active_snapshot.is_none() || snapshot.sequence > snapshot_sequence {
                    snapshot_sequence = snapshot.sequence;
                    active_snapshot = Some(slot);
                    last = Some(snapshot.state);
                }
            }
        }
        let end = unsafe { libc::lseek(fd.as_raw_fd(), 0, libc::SEEK_END) };
        if end < 0 || end as usize > MAX_WAL {
            return Err(VarRunError::protocol("state WAL budget"));
        }
        let mut data = vec![0; end as usize];
        unsafe { libc::lseek(fd.as_raw_fd(), 0, libc::SEEK_SET) };
        let mut n = 0;
        while n < data.len() {
            let k = unsafe {
                libc::read(
                    fd.as_raw_fd(),
                    data[n..].as_mut_ptr().cast(),
                    data.len() - n,
                )
            };
            if k < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            if k <= 0 {
                return Err(VarRunError::io("read state WAL"));
            }
            n += k as usize
        }
        let mut good = 0;
        let mut last_line = None;
        for line in data.split_inclusive(|b| *b == b'\n') {
            if line.last() != Some(&b'\n') {
                break;
            }
            serde_json::from_slice::<serde_json::Value>(&line[..line.len() - 1])
                .map_err(|_| VarRunError::protocol("malformed state WAL"))?;
            last_line = Some(&line[..line.len() - 1]);
            good += line.len()
        }
        if let Some(line) = last_line {
            last = Some(decode_state(line)?);
        }
        if good != data.len()
            && (unsafe { libc::ftruncate(fd.as_raw_fd(), good as _) } != 0
                || unsafe { libc::fsync(fd.as_raw_fd()) } != 0)
        {
            return Err(VarRunError::io("repair torn WAL"));
        }
        unsafe { libc::lseek(fd.as_raw_fd(), 0, libc::SEEK_END) };
        Ok((
            Self {
                fd,
                len: good,
                parent: prefix,
                identity: i,
                snapshots,
                snapshot_sequence,
                active_snapshot,
                last: last.clone(),
            },
            last,
        ))
    }
    fn compact(&mut self, mutated: bool) -> Result<(), VarRunError> {
        let state = self
            .last
            .clone()
            .ok_or_else(|| VarRunError::protocol("compact empty WAL"))?;
        let target = self.active_snapshot.map_or(0, |slot| 1 - slot);
        let (slot, expected) = &self.snapshots[target];
        let name = if target == 0 { SNAPSHOT_A } else { SNAPSHOT_B };
        if id(slot.as_raw_fd())? != *expected || named(self.parent, name)? != Some(*expected) {
            return Err(if mutated {
                VarRunError::recovery("snapshot slot replacement")
            } else {
                VarRunError::protocol("snapshot slot replacement")
            });
        }
        let snapshot = Snapshot {
            schema_version: 1,
            sequence: self.snapshot_sequence.saturating_add(1),
            state,
        };
        let mut bytes = serde_json::to_vec(&snapshot)
            .map_err(|_| VarRunError::protocol("encode state snapshot"))?;
        bytes.push(b'\n');
        if bytes.len() > MAX_SNAPSHOT {
            return Err(VarRunError::recovery("state snapshot budget"));
        }
        if mutated {
            checkpoint(30)?;
        }
        if unsafe { libc::ftruncate(slot.as_raw_fd(), 0) } != 0 {
            return Err(VarRunError::io("truncate inactive snapshot").owning());
        }
        if unsafe { libc::lseek(slot.as_raw_fd(), 0, libc::SEEK_SET) } != 0 {
            return Err(VarRunError::io("rewind inactive snapshot").owning());
        }
        if mutated {
            checkpoint(31)?;
        }
        write_all(slot.as_raw_fd(), &bytes, "write state snapshot")?;
        if mutated {
            checkpoint(32)?;
        }
        if unsafe { libc::fsync(slot.as_raw_fd()) } != 0 {
            return Err(VarRunError::io("fsync state snapshot").owning());
        }
        self.snapshot_sequence = snapshot.sequence;
        self.active_snapshot = Some(target);
        if mutated {
            checkpoint(33)?;
        }
        if unsafe { libc::ftruncate(self.fd.as_raw_fd(), 0) } != 0 {
            return Err(VarRunError::io("truncate compacted WAL").owning());
        }
        self.len = 0;
        if mutated {
            checkpoint(34)?;
        }
        if unsafe { libc::fsync(self.fd.as_raw_fd()) } != 0 {
            return Err(VarRunError::io("fsync compacted WAL").owning());
        }
        if mutated {
            checkpoint(35)?;
        }
        Ok(())
    }
    fn put(&mut self, s: &State, mutated: bool) -> Result<(), VarRunError> {
        if id(self.fd.as_raw_fd())? != self.identity
            || named(self.parent, STATE)? != Some(self.identity)
        {
            return Err(if mutated {
                VarRunError::recovery("state WAL replacement")
            } else {
                VarRunError::protocol("state WAL replacement")
            });
        }
        let mut d = serde_json::to_vec(s).map_err(|_| VarRunError::protocol("encode state"))?;
        d.push(b'\n');
        if self.len + d.len() > MAX_WAL {
            self.compact(mutated)?;
        }
        if d.len() > MAX_WAL {
            return Err(VarRunError::recovery("state WAL record budget"));
        }
        if mutated {
            checkpoint(20)?;
        }
        let mut n = 0;
        while n < d.len() {
            let k =
                unsafe { libc::write(self.fd.as_raw_fd(), d[n..].as_ptr().cast(), d.len() - n) };
            if k < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            if k <= 0 {
                return Err(VarRunError::io("write state WAL").owning());
            }
            n += k as usize
        }
        if mutated {
            checkpoint(21)?;
        }
        if unsafe { libc::fsync(self.fd.as_raw_fd()) } != 0 {
            return Err(VarRunError::io("fsync state WAL").owning());
        }
        self.len += d.len();
        self.last = Some(s.clone());
        Ok(())
    }
}

fn decode_state(bytes: &[u8]) -> Result<State, VarRunError> {
    if let Ok(state) = serde_json::from_slice(bytes) {
        return Ok(state);
    }
    let value: serde_json::Value =
        serde_json::from_slice(bytes).map_err(|_| VarRunError::protocol("malformed state WAL"))?;
    let forensic = value
        .get("forensic")
        .and_then(serde_json::Value::as_array)
        .ok_or_else(|| VarRunError::protocol("malformed forensic state"))?;
    if forensic.len() > MAX_FORENSIC || !forensic.iter().all(serde_json::Value::is_string) {
        return Err(VarRunError::protocol("malformed legacy forensic state"));
    }
    Err(VarRunError::recovery("legacy forensic records are unbound"))
}

fn open_state_file(
    parent: RawFd,
    name: &CStr,
    owner: Id,
    append: bool,
    points: [u8; 4],
) -> Result<(OwnedFd, Id), VarRunError> {
    let extra = if append { libc::O_APPEND } else { 0 };
    let mut created = false;
    let mut fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDWR | libc::O_NONBLOCK | libc::O_NOFOLLOW | libc::O_CLOEXEC | extra,
        )
    };
    if fd < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
        checkpoint(points[0])?;
        fd = unsafe {
            libc::openat(
                parent,
                name.as_ptr(),
                libc::O_RDWR
                    | libc::O_NONBLOCK
                    | libc::O_NOFOLLOW
                    | libc::O_CLOEXEC
                    | libc::O_CREAT
                    | libc::O_EXCL
                    | extra,
                0o600,
            )
        };
        created = fd >= 0;
        if created {
            checkpoint(points[1])?;
        }
    }
    if fd < 0 {
        return Err(VarRunError::io("open state file"));
    }
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    if created && unsafe { libc::fchmod(fd.as_raw_fd(), 0o600) } != 0 {
        return Err(VarRunError::io("normalize state file"));
    }
    let identity = id(fd.as_raw_fd())?;
    if identity.mode & libc::S_IFMT != libc::S_IFREG
        || identity.mode & 0o777 != 0o600
        || identity.uid != owner.uid
        || identity.gid != owner.gid
        || identity.nlink != 1
        || named(parent, name)? != Some(identity)
    {
        return Err(VarRunError::protocol("invalid state file"));
    }
    if created {
        checkpoint(points[2])?;
        if unsafe { libc::fsync(fd.as_raw_fd()) } != 0 {
            return Err(VarRunError::io("fsync new state file").owning());
        }
        checkpoint(points[3])?;
    }
    Ok((fd, identity))
}

fn read_snapshot(fd: RawFd) -> Result<Option<Snapshot>, VarRunError> {
    let end = unsafe { libc::lseek(fd, 0, libc::SEEK_END) };
    if end < 0 || end as usize > MAX_SNAPSHOT {
        return Err(VarRunError::protocol("snapshot budget"));
    }
    if end == 0 {
        return Ok(None);
    }
    let mut bytes = vec![0; end as usize];
    unsafe { libc::lseek(fd, 0, libc::SEEK_SET) };
    let mut offset = 0;
    while offset < bytes.len() {
        let got = unsafe {
            libc::read(
                fd,
                bytes[offset..].as_mut_ptr().cast(),
                bytes.len() - offset,
            )
        };
        if got < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        if got <= 0 {
            return Err(VarRunError::io("read state snapshot"));
        }
        offset += got as usize;
    }
    if bytes.last() != Some(&b'\n') {
        return Ok(None);
    }
    serde_json::from_slice(&bytes[..bytes.len() - 1])
        .map(Some)
        .map_err(|_| VarRunError::protocol("malformed state snapshot"))
}

fn write_all(fd: RawFd, bytes: &[u8], op: &'static str) -> Result<(), VarRunError> {
    let mut offset = 0;
    while offset < bytes.len() {
        let wrote =
            unsafe { libc::write(fd, bytes[offset..].as_ptr().cast(), bytes.len() - offset) };
        if wrote < 0 && io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
            continue;
        }
        if wrote <= 0 {
            return Err(VarRunError::io(op).owning());
        }
        offset += wrote as usize;
    }
    Ok(())
}

fn remove_tree(
    fd: RawFd,
    depth: usize,
    count: &mut usize,
    deadline: Instant,
) -> Result<(), VarRunError> {
    if depth > MAX_DEPTH || *count > MAX_ENTRIES || Instant::now() > deadline {
        return Err(VarRunError::recovery("cleanup budget"));
    }
    let scan = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 3) };
    if scan < 0 {
        return Err(VarRunError::io("duplicate cleanup directory").owning());
    }
    let dir = unsafe { libc::fdopendir(scan) };
    if dir.is_null() {
        unsafe { libc::close(scan) };
        return Err(VarRunError::io("scan cleanup directory").owning());
    }
    let dir = DirStream(dir);
    loop {
        unsafe { *libc::__errno_location() = 0 };
        let e = unsafe { libc::readdir(dir.0) };
        if e.is_null() {
            let errno = io::Error::last_os_error();
            if errno.raw_os_error().unwrap_or(0) != 0 {
                return Err(VarRunError {
                    op: "read cleanup directory",
                    source: Some(errno),
                    recovery: true,
                });
            }
            break;
        }
        let n = unsafe { CStr::from_ptr((*e).d_name.as_ptr()) };
        if n.to_bytes() == b"." || n.to_bytes() == b".." {
            continue;
        }
        *count += 1;
        if *count > MAX_ENTRIES || Instant::now() > deadline {
            return Err(VarRunError::recovery("cleanup budget"));
        }
        let before = named(fd, n)?.ok_or_else(|| VarRunError::recovery("entry disappeared"))?;
        if before.mode & libc::S_IFMT == libc::S_IFDIR {
            let child = open_dir(fd, n).map_err(VarRunError::owning)?;
            if id(child.as_raw_fd())? != before {
                return Err(VarRunError::recovery("child replacement"));
            }
            remove_tree(child.as_raw_fd(), depth + 1, count, deadline)?;
            if named(fd, n)? != Some(before)
                || unsafe { libc::unlinkat(fd, n.as_ptr(), libc::AT_REMOVEDIR) } != 0
            {
                return Err(VarRunError::recovery("child collection failed"));
            }
        } else {
            let x = unsafe {
                libc::openat(
                    fd,
                    n.as_ptr(),
                    libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            if x < 0 {
                return Err(VarRunError::io("retain cleanup leaf").owning());
            }
            let x = unsafe { OwnedFd::from_raw_fd(x) };
            if id(x.as_raw_fd())? != before
                || named(fd, n)? != Some(before)
                || unsafe { libc::unlinkat(fd, n.as_ptr(), 0) } != 0
            {
                return Err(VarRunError::recovery("leaf collection failed"));
            }
        }
    }
    Ok(())
}

fn mount_id(fd: RawFd) -> Result<u64, VarRunError> {
    let mut value = MaybeUninit::<libc::statx>::zeroed();
    if unsafe {
        libc::statx(
            fd,
            c"".as_ptr(),
            libc::AT_EMPTY_PATH | libc::AT_SYMLINK_NOFOLLOW,
            libc::STATX_MNT_ID,
            value.as_mut_ptr(),
        )
    } != 0
    {
        return Err(VarRunError::io("statx cleanup mount"));
    }
    Ok(unsafe { value.assume_init() }.stx_mnt_id)
}
fn plan_tree(
    fd: RawFd,
    depth: usize,
    plan: &mut Vec<Id>,
    mount: u64,
    deadline: Instant,
) -> Result<(), VarRunError> {
    if depth > MAX_DEPTH || plan.len() > MAX_ENTRIES || Instant::now() > deadline {
        return Err(VarRunError::protocol("var/run plan budget"));
    }
    let root = id(fd)?;
    if mount_id(fd)? != mount {
        return Err(VarRunError::protocol("mounted object beneath var/run"));
    }
    plan.push(root);
    let scan = unsafe { libc::fcntl(fd, libc::F_DUPFD_CLOEXEC, 3) };
    if scan < 0 {
        return Err(VarRunError::io("duplicate plan directory"));
    }
    let dir = unsafe { libc::fdopendir(scan) };
    if dir.is_null() {
        unsafe { libc::close(scan) };
        return Err(VarRunError::io("scan cleanup plan"));
    }
    let dir = DirStream(dir);
    loop {
        unsafe { *libc::__errno_location() = 0 };
        let ent = unsafe { libc::readdir(dir.0) };
        if ent.is_null() {
            let e = io::Error::last_os_error();
            if e.raw_os_error().unwrap_or(0) != 0 {
                return Err(VarRunError {
                    op: "read cleanup plan",
                    source: Some(e),
                    recovery: false,
                });
            }
            break;
        }
        let name = unsafe { CStr::from_ptr((*ent).d_name.as_ptr()) };
        if name.to_bytes() == b"." || name.to_bytes() == b".." {
            continue;
        }
        if plan.len() >= MAX_ENTRIES {
            return Err(VarRunError::protocol("var/run plan event budget"));
        }
        let leaf = unsafe {
            libc::openat(
                fd,
                name.as_ptr(),
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if leaf < 0 {
            return Err(VarRunError::io("retain cleanup plan object"));
        }
        let leaf = unsafe { OwnedFd::from_raw_fd(leaf) };
        let observed = id(leaf.as_raw_fd())?;
        if mount_id(leaf.as_raw_fd())? != mount {
            return Err(VarRunError::protocol("mounted object beneath var/run"));
        }
        if observed.mode & libc::S_IFMT == libc::S_IFDIR {
            let child = open_dir(fd, name)?;
            plan_tree(child.as_raw_fd(), depth + 1, plan, mount, deadline)?
        } else {
            plan.push(observed)
        }
    }
    Ok(())
}
fn numeric(name: &CStr) -> bool {
    !name.to_bytes().is_empty() && name.to_bytes().iter().all(u8::is_ascii_digit)
}
fn reject_holders(plan: &[Id], deadline: Instant) -> Result<(), VarRunError> {
    reject_holders_with_limits(plan, deadline, MAX_PROCESSES, MAX_FDS)
}
fn reject_holders_with_limits(
    plan: &[Id],
    deadline: Instant,
    max_processes: usize,
    max_fds: usize,
) -> Result<(), VarRunError> {
    let proc_fd = unsafe {
        libc::open(
            c"/proc".as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    if proc_fd < 0 {
        return Err(VarRunError::io("open proc holder census"));
    }
    let proc_fd = unsafe { OwnedFd::from_raw_fd(proc_fd) };
    let scan = unsafe { libc::fcntl(proc_fd.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) };
    if scan < 0 {
        return Err(VarRunError::io("duplicate proc census"));
    }
    let processes_dir = unsafe { libc::fdopendir(scan) };
    if processes_dir.is_null() {
        unsafe { libc::close(scan) };
        return Err(VarRunError::io("scan proc census"));
    }
    let processes_dir = DirStream(processes_dir);
    let (mut processes, mut fds) = (0usize, 0usize);
    loop {
        unsafe { *libc::__errno_location() = 0 };
        let entry = unsafe { libc::readdir(processes_dir.0) };
        if entry.is_null() {
            let error = io::Error::last_os_error();
            return if error.raw_os_error().unwrap_or(0) == 0 {
                Ok(())
            } else {
                Err(VarRunError {
                    op: "read proc census",
                    source: Some(error),
                    recovery: false,
                })
            };
        }
        let pid = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
        if !numeric(pid) {
            continue;
        }
        processes += 1;
        if processes > max_processes || Instant::now() > deadline {
            return Err(VarRunError::protocol("process census budget"));
        }
        let process = unsafe {
            libc::openat(
                proc_fd.as_raw_fd(),
                pid.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if process < 0 {
            continue;
        }
        let process = unsafe { OwnedFd::from_raw_fd(process) };
        for special in [c"cwd", c"root"] {
            let held = unsafe {
                libc::openat(
                    process.as_raw_fd(),
                    special.as_ptr(),
                    libc::O_PATH | libc::O_CLOEXEC,
                )
            };
            if held >= 0 {
                let held = unsafe { OwnedFd::from_raw_fd(held) };
                let observed = match id(held.as_raw_fd()) {
                    Ok(value) => value,
                    Err(_) => continue,
                };
                if plan
                    .iter()
                    .any(|candidate| same_object(*candidate, observed))
                {
                    return Err(VarRunError::protocol("active process retains var/run"));
                }
            }
        }
        let fd_parent = unsafe {
            libc::openat(
                process.as_raw_fd(),
                c"fd".as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd_parent < 0 {
            continue;
        }
        let fd_parent = unsafe { OwnedFd::from_raw_fd(fd_parent) };
        let fd_scan = unsafe { libc::fcntl(fd_parent.as_raw_fd(), libc::F_DUPFD_CLOEXEC, 3) };
        if fd_scan < 0 {
            return Err(VarRunError::io("duplicate fd census"));
        }
        let entries = unsafe { libc::fdopendir(fd_scan) };
        if entries.is_null() {
            unsafe { libc::close(fd_scan) };
            return Err(VarRunError::io("scan fd census"));
        }
        let entries = DirStream(entries);
        loop {
            unsafe { *libc::__errno_location() = 0 };
            let entry = unsafe { libc::readdir(entries.0) };
            if entry.is_null() {
                let error = io::Error::last_os_error();
                if error.raw_os_error().unwrap_or(0) != 0 {
                    return Err(VarRunError {
                        op: "read process fd census",
                        source: Some(error),
                        recovery: false,
                    });
                }
                break;
            }
            let number = unsafe { CStr::from_ptr((*entry).d_name.as_ptr()) };
            if !numeric(number) {
                continue;
            }
            fds += 1;
            if fds > max_fds || Instant::now() > deadline {
                return Err(VarRunError::protocol("fd census budget"));
            }
            let held = unsafe {
                libc::openat(
                    fd_parent.as_raw_fd(),
                    number.as_ptr(),
                    libc::O_PATH | libc::O_CLOEXEC,
                )
            };
            if held < 0 {
                continue;
            }
            let held = unsafe { OwnedFd::from_raw_fd(held) };
            let observed = match id(held.as_raw_fd()) {
                Ok(value) => value,
                Err(_) => continue,
            };
            if plan
                .iter()
                .any(|candidate| same_object(*candidate, observed))
            {
                return Err(VarRunError::protocol("fd holder retains var/run"));
            }
        }
    }
}
fn preflight_tree(parent: RawFd, name: &CStr, expected: Id) -> Result<(), VarRunError> {
    let directory = open_dir(parent, name)?;
    if !same_object(id(directory.as_raw_fd())?, expected) {
        return Err(VarRunError::recovery("var/run changed before rotation"));
    }
    let mut plan = Vec::new();
    let deadline = Instant::now() + Duration::from_secs(2);
    plan_tree(
        directory.as_raw_fd(),
        0,
        &mut plan,
        mount_id(directory.as_raw_fd())?,
        deadline,
    )?;
    drop(directory);
    reject_holders(&plan, deadline)
}

fn resume(
    prefix: RawFd,
    var: RawFd,
    owner: Id,
    var_id: Id,
    w: &mut Wal,
    mut s: State,
) -> Result<State, VarRunError> {
    loop {
        revalidate(prefix, owner, var, var_id)?;
        validate_forensic(var, &s)?;
        match s.phase {
            Phase::Active => return Ok(s),
            Phase::MovePending => {
                checkpoint(1)?;
                let n = cname(&s.old_name)?;
                let old = s
                    .old_id
                    .ok_or_else(|| VarRunError::recovery("missing old identity"))?;
                let p = var;
                match (named(var, RUN)?, named(p, &n)?) {
                    (Some(x), None) if same_object(x, old) => {
                        preflight_tree(var, RUN, old)?;
                        rename_nr(var, RUN, p, &n).map_err(VarRunError::owning)?;
                        checkpoint(2)?;
                        sync(var).map_err(VarRunError::owning)?;
                    }
                    (None, Some(x)) if same_object(x, old) => {}
                    _ => return Err(VarRunError::recovery("ambiguous move recovery")),
                }
                s.phase = Phase::Moved;
                w.put(&s, true)?
            }
            Phase::Moved => {
                s.phase = Phase::CreatePending;
                w.put(&s, true)?
            }
            Phase::CreatePending => {
                checkpoint(3)?;
                let n = cname(&s.new_name)?;
                if let Some(x) = named(var, &n)? {
                    if s.forensic.len() >= MAX_FORENSIC {
                        return Err(VarRunError::recovery("forensic budget"));
                    }
                    let q = transition("darling-var-run-unknown", s.generation, x);
                    let q_name = q.to_string_lossy().into_owned();
                    if !s.forensic.iter().any(|record| record.name == q_name) {
                        s.forensic.push(ForensicRecord {
                            name: q_name,
                            identity: x,
                        });
                        // Persist the exact forensic destination before moving
                        // an object whose creation identity was never committed.
                        w.put(&s, true)?;
                    }
                    rename_nr(var, &n, var, &q).map_err(VarRunError::owning)?;
                    sync(var).map_err(VarRunError::owning)?;
                    s.new_name = Some(fresh(s.generation)?.to_string_lossy().into_owned());
                    w.put(&s, true)?;
                    continue;
                }
                if unsafe { libc::mkdirat(var, n.as_ptr(), 0o700) } != 0 {
                    return Err(VarRunError::io("create var/run").owning());
                }
                checkpoint(4)?;
                let d = open_dir(var, &n).map_err(VarRunError::owning)?;
                if unsafe { libc::fchown(d.as_raw_fd(), owner.uid, owner.gid) } != 0
                    || unsafe { libc::fchmod(d.as_raw_fd(), 0o755) } != 0
                {
                    return Err(VarRunError::io("normalize var/run").owning());
                }
                let x = id(d.as_raw_fd())?;
                if !valid_dir(x, owner) {
                    return Err(VarRunError::recovery("invalid created var/run"));
                }
                sync(d.as_raw_fd()).map_err(VarRunError::owning)?;
                sync(var).map_err(VarRunError::owning)?;
                s.new_id = Some(x);
                s.phase = Phase::Created;
                w.put(&s, true)?
            }
            Phase::Created => {
                checkpoint(5)?;
                let n = cname(&s.new_name)?;
                if named(var, &n)? != s.new_id {
                    return Err(VarRunError::recovery("created replacement"));
                }
                s.phase = Phase::PublishPending;
                w.put(&s, true)?
            }
            Phase::PublishPending => {
                let n = cname(&s.new_name)?;
                let x = s
                    .new_id
                    .ok_or_else(|| VarRunError::recovery("missing new identity"))?;
                match (named(var, &n)?, named(var, RUN)?) {
                    (Some(a), None) if a == x => {
                        rename_nr(var, &n, var, RUN).map_err(VarRunError::owning)?;
                        checkpoint(6)?;
                        sync(var).map_err(VarRunError::owning)?
                    }
                    (None, Some(a)) if a == x => {}
                    _ => return Err(VarRunError::recovery("ambiguous publish recovery")),
                }
                s.public = Some(x);
                s.phase = Phase::Published;
                w.put(&s, true)?
            }
            Phase::Published => {
                if named(var, RUN)? != s.public {
                    return Err(VarRunError::recovery("public replacement"));
                }
                if s.collect {
                    s.phase = Phase::CollectPending
                } else {
                    s.phase = Phase::Active;
                    s.old_name = None;
                    s.old_id = None;
                    s.new_name = None;
                    s.new_id = None
                }
                w.put(&s, true)?
            }
            Phase::CollectPending => {
                checkpoint(7)?;
                let n = cname(&s.old_name)?;
                let x = s
                    .old_id
                    .ok_or_else(|| VarRunError::recovery("missing collection identity"))?;
                match named(var, &n)? {
                    None => {}
                    Some(y) if same_object(y, x) => {
                        preflight_tree(var, &n, x).map_err(VarRunError::owning)?;
                        let d = open_dir(var, &n).map_err(VarRunError::owning)?;
                        if !same_object(id(d.as_raw_fd())?, x) {
                            return Err(VarRunError::recovery("collection replacement"));
                        }
                        let mut count = 0;
                        remove_tree(
                            d.as_raw_fd(),
                            0,
                            &mut count,
                            Instant::now() + Duration::from_secs(2),
                        )?;
                        if !named(var, &n)?.is_some_and(|named| same_object(named, x))
                            || unsafe { libc::unlinkat(var, n.as_ptr(), libc::AT_REMOVEDIR) } != 0
                        {
                            return Err(VarRunError::recovery("collection failed"));
                        }
                        checkpoint(8)?;
                        sync(var).map_err(VarRunError::owning)?
                    }
                    _ => return Err(VarRunError::recovery("collection replacement")),
                }
                s.phase = Phase::Active;
                s.collect = false;
                s.old_name = None;
                s.old_id = None;
                s.new_name = None;
                s.new_id = None;
                w.put(&s, true)?
            }
        }
    }
}

/// Acquire the exact `var` parent from a retained prefix capability.
pub(crate) fn acquire_var_parent(prefix: RawFd) -> Result<OwnedFd, VarRunError> {
    let root = id(prefix)?;
    if !valid_dir(root, root) {
        return Err(VarRunError::protocol("invalid retained prefix"));
    }
    let var = open_dir(prefix, VAR)?;
    let var_id = id(var.as_raw_fd())?;
    if !valid_dir(var_id, root)
        || !named(prefix, VAR)?.is_some_and(|named| same_authority(named, var_id))
    {
        return Err(VarRunError::protocol("invalid var parent"));
    }
    Ok(var)
}

/// Rotate/create `var/run` through retained prefix and parent capabilities.
/// Caller owns the exact exclusive lifecycle lease for their full lifetime.
pub(crate) fn prepare_retained(
    prefix: RawFd,
    var: RawFd,
    generation: u64,
) -> Result<VarRunOutcome, VarRunError> {
    if generation == 0 {
        return Err(VarRunError::protocol("zero generation"));
    }
    let root = id(prefix)?;
    if !valid_dir(root, root) {
        return Err(VarRunError::protocol("invalid retained prefix"));
    }
    let var_id = id(var)?;
    if !valid_dir(var_id, root)
        || !named(prefix, VAR)?.is_some_and(|named| same_authority(named, var_id))
    {
        return Err(VarRunError::protocol("invalid retained var parent"));
    }
    let (mut w, last) = Wal::open(prefix, var, root)?;
    if let Some(s) = &last {
        if s.schema_version != 3
            || !same_authority(s.prefix, root)
            || !same_authority(s.var_parent, var_id)
            || s.forensic.len() > MAX_FORENSIC
        {
            return Err(VarRunError::protocol("stale state authority"));
        }
        validate_forensic(var, s)?;
    }
    let last = match last {
        Some(s) if s.phase != Phase::Active => Some(resume(prefix, var, root, var_id, &mut w, s)?),
        x => x,
    };
    if let Some(active) = &last {
        if active.phase == Phase::Active && active.generation == generation {
            let public = active
                .public
                .ok_or_else(|| VarRunError::recovery("missing active identity"))?;
            if !named(var, RUN)?.is_some_and(|named| same_object(named, public)) {
                return Err(VarRunError::recovery("active var/run identity mismatch"));
            }
            return Ok(VarRunOutcome {
                generation,
                identity: (public.dev, public.ino),
                forensic_quarantine: active.forensic.last().map(|record| record.name.clone()),
            });
        }
    }
    let current = named(var, RUN)?;
    let mut forensic = last
        .as_ref()
        .map(|s| s.forensic.clone())
        .unwrap_or_default();
    let (old_name, old_id, collect) = match current {
        None => (None, None, false),
        Some(x) => {
            if !valid_dir(x, root) {
                return Err(VarRunError::protocol("invalid public var/run"));
            }
            let bound = last
                .as_ref()
                .and_then(|s| s.public)
                .filter(|y| same_object(*y, x));
            if last.is_some() && bound.is_none() {
                return Err(VarRunError::protocol("public identity differs from state"));
            }
            let collect = bound.is_some();
            if !collect && forensic.len() >= MAX_FORENSIC {
                return Err(VarRunError::protocol("forensic budget"));
            }
            let n = if collect {
                transition("run.gc", last.as_ref().unwrap().generation, x)
            } else {
                transition("darling-var-run-forensic", generation, x)
            };
            if !collect {
                forensic.push(ForensicRecord {
                    name: n.to_string_lossy().into_owned(),
                    identity: x,
                })
            }
            (Some(n.to_string_lossy().into_owned()), Some(x), collect)
        }
    };
    let s = State {
        schema_version: 3,
        prefix: root,
        var_parent: var_id,
        generation,
        phase: if old_id.is_some() {
            Phase::MovePending
        } else {
            Phase::Moved
        },
        public: None,
        old_name,
        old_id,
        collect,
        new_name: Some(fresh(generation)?.to_string_lossy().into_owned()),
        new_id: None,
        forensic,
    };
    w.put(&s, false)?;
    let s = resume(prefix, var, root, var_id, &mut w, s)?;
    let x = s
        .public
        .ok_or_else(|| VarRunError::recovery("missing active identity"))?;
    Ok(VarRunOutcome {
        generation,
        identity: (x.dev, x.ino),
        forensic_quarantine: s.forensic.last().map(|record| record.name.clone()),
    })
}

#[cfg(test)]
fn prepare(prefix: RawFd, generation: u64) -> Result<VarRunOutcome, VarRunError> {
    let var = acquire_var_parent(prefix)?;
    prepare_retained(prefix, var.as_raw_fd(), generation)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        fs,
        os::unix::fs::{MetadataExt, PermissionsExt},
        path::PathBuf,
        sync::{Mutex, MutexGuard},
    };

    static FIXTURE_SERIAL: Mutex<()> = Mutex::new(());

    struct Fixture {
        path: PathBuf,
        root: OwnedFd,
        _serial: Option<MutexGuard<'static, ()>>,
    }
    impl Fixture {
        fn new() -> Self {
            let guard = FIXTURE_SERIAL
                .lock()
                .unwrap_or_else(|error| error.into_inner());
            Self::with_guard(Some(guard))
        }
        fn new_unlocked() -> Self {
            Self::with_guard(None)
        }
        fn with_guard(guard: Option<MutexGuard<'static, ()>>) -> Self {
            let path = std::env::temp_dir().join(format!(
                "dar-awrp-2-{}-{}",
                unsafe { libc::getpid() },
                fresh(1).unwrap().to_string_lossy()
            ));
            fs::create_dir(&path).unwrap();
            fs::create_dir(path.join("var")).unwrap();
            let c = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
            let fd = unsafe {
                libc::open(
                    c.as_ptr(),
                    libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
                )
            };
            Self {
                path,
                root: unsafe { OwnedFd::from_raw_fd(fd) },
                _serial: guard,
            }
        }
        fn run(&self) -> PathBuf {
            self.path.join("var/run")
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.path);
        }
    }

    #[test]
    fn absent_create_is_umask_independent_and_tmp_survives() {
        let f = Fixture::new();
        fs::create_dir(f.path.join("var/tmp")).unwrap();
        fs::write(f.path.join("var/tmp/sentinel"), b"keep").unwrap();
        let before = fs::metadata(f.path.join("var/tmp/sentinel")).unwrap();
        let old = unsafe { libc::umask(0o077) };
        let out = prepare(f.root.as_raw_fd(), 11).unwrap();
        unsafe { libc::umask(old) };
        let run = fs::metadata(f.run()).unwrap();
        assert_eq!(run.mode() & 0o777, 0o755);
        assert_eq!((run.dev(), run.ino()), out.identity);
        let after = fs::metadata(f.path.join("var/tmp/sentinel")).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        assert_eq!(fs::read(f.path.join("var/tmp/sentinel")).unwrap(), b"keep");
    }

    #[test]
    fn legacy_directory_is_preserved_and_restart_rotates_bound_generation() {
        let f = Fixture::new();
        fs::create_dir(f.run()).unwrap();
        fs::write(f.run().join("legacy"), b"evidence").unwrap();
        let legacy = fs::metadata(f.run()).unwrap();
        let first = prepare(f.root.as_raw_fd(), 1).unwrap();
        let q = f.path.join("var").join(first.forensic_quarantine.unwrap());
        let qm = fs::metadata(&q).unwrap();
        assert_eq!((legacy.dev(), legacy.ino()), (qm.dev(), qm.ino()));
        assert_eq!(fs::read(q.join("legacy")).unwrap(), b"evidence");
        fs::write(f.run().join("owned"), b"old").unwrap();
        let second = prepare(f.root.as_raw_fd(), 2).unwrap();
        assert_ne!(first.identity, second.identity);
        assert!(!f.path.join("var").read_dir().unwrap().any(|e| e
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".run.gc")));
    }

    #[test]
    fn unbound_special_objects_are_rejected_without_mutation() {
        let f = Fixture::new();
        fs::write(f.run(), b"foreign").unwrap();
        let before = fs::symlink_metadata(f.run()).unwrap();
        let error = prepare(f.root.as_raw_fd(), 3).unwrap_err();
        assert!(!error.recovery_required());
        let after = fs::symlink_metadata(f.run()).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        assert_eq!(fs::read(f.run()).unwrap(), b"foreign");
    }

    #[test]
    fn fifo_symlink_and_socket_are_rejected_without_blocking_or_mutation() {
        use std::os::unix::{fs::symlink, net::UnixListener};
        for kind in 0..3 {
            let f = Fixture::new();
            match kind {
                0 => {
                    let n = CString::new(f.run().as_os_str().as_encoded_bytes()).unwrap();
                    assert_eq!(unsafe { libc::mkfifo(n.as_ptr(), 0o600) }, 0)
                }
                1 => symlink("foreign", f.run()).unwrap(),
                _ => drop(UnixListener::bind(f.run()).unwrap()),
            }
            let before = fs::symlink_metadata(f.run()).unwrap();
            let error = prepare(f.root.as_raw_fd(), 9).unwrap_err();
            assert!(!error.recovery_required());
            let after = fs::symlink_metadata(f.run()).unwrap();
            assert_eq!(
                (before.dev(), before.ino(), before.file_type()),
                (after.dev(), after.ino(), after.file_type())
            );
        }
    }

    #[test]
    fn depth_budget_preserves_owned_quarantine_for_recovery() {
        let f = Fixture::new();
        prepare(f.root.as_raw_fd(), 80).unwrap();
        let mut at = f.run();
        for _ in 0..=MAX_DEPTH {
            at = at.join("d");
            fs::create_dir(&at).unwrap();
        }
        let error = prepare(f.root.as_raw_fd(), 81).unwrap_err();
        assert!(!error.recovery_required(), "{error}");
        assert!(f.run().is_dir());
        assert!(!f.path.join("var").read_dir().unwrap().any(|e| e
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with(".run.gc")));
    }

    #[test]
    fn event_time_process_and_fd_budgets_fail_closed() {
        let f = Fixture::new();
        let var = open_dir(f.root.as_raw_fd(), VAR).unwrap();
        let mut overfull = vec![id(var.as_raw_fd()).unwrap(); MAX_ENTRIES + 1];
        let error = plan_tree(
            var.as_raw_fd(),
            0,
            &mut overfull,
            mount_id(var.as_raw_fd()).unwrap(),
            Instant::now() + Duration::from_secs(1),
        )
        .unwrap_err();
        assert!(!error.recovery_required());

        let mut empty = Vec::new();
        let error = plan_tree(
            var.as_raw_fd(),
            0,
            &mut empty,
            mount_id(var.as_raw_fd()).unwrap(),
            Instant::now() - Duration::from_millis(1),
        )
        .unwrap_err();
        assert!(!error.recovery_required());

        assert!(reject_holders_with_limits(
            &[],
            Instant::now() + Duration::from_secs(1),
            0,
            MAX_FDS
        )
        .is_err());
        assert!(reject_holders_with_limits(
            &[],
            Instant::now() + Duration::from_secs(1),
            MAX_PROCESSES,
            0,
        )
        .is_err());
        assert!(!f.run().exists());
    }

    #[test]
    fn retained_fd_holder_rejects_rotation_before_namespace_mutation() {
        let f = Fixture::new();
        fs::create_dir(f.run()).unwrap();
        fs::write(f.run().join("held"), b"live").unwrap();
        let held = fs::File::open(f.run().join("held")).unwrap();
        let before = fs::metadata(f.run()).unwrap();
        let error = prepare(f.root.as_raw_fd(), 90).unwrap_err();
        assert!(!error.recovery_required());
        let after = fs::metadata(f.run()).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        drop(held);
        assert!(prepare(f.root.as_raw_fd(), 90).is_ok());
    }

    #[test]
    fn prefix_parent_wal_and_public_replacements_fail_closed() {
        // A pathname replacement of the prefix cannot redirect retained authority.
        let f = Fixture::new();
        let original = f.path.with_extension("retained");
        fs::rename(&f.path, &original).unwrap();
        fs::create_dir(&f.path).unwrap();
        fs::create_dir(f.path.join("var")).unwrap();
        let replacement = fs::metadata(&f.path).unwrap();
        assert!(prepare(f.root.as_raw_fd(), 100).is_ok());
        assert_eq!(
            (
                fs::metadata(&f.path).unwrap().dev(),
                fs::metadata(&f.path).unwrap().ino()
            ),
            (replacement.dev(), replacement.ino())
        );
        fs::remove_dir_all(&f.path).unwrap();
        fs::rename(&original, &f.path).unwrap();

        prepare(f.root.as_raw_fd(), 101).unwrap();
        let old_var = f.path.join("var-old");
        fs::rename(f.path.join("var"), &old_var).unwrap();
        fs::create_dir(f.path.join("var")).unwrap();
        let new_var = fs::metadata(f.path.join("var")).unwrap();
        assert!(prepare(f.root.as_raw_fd(), 102).is_err());
        assert_eq!(
            (
                fs::metadata(f.path.join("var")).unwrap().dev(),
                fs::metadata(f.path.join("var")).unwrap().ino()
            ),
            (new_var.dev(), new_var.ino())
        );
        fs::remove_dir(f.path.join("var")).unwrap();
        fs::rename(&old_var, f.path.join("var")).unwrap();

        let public = f.run();
        fs::rename(&public, f.path.join("var/run-bound")).unwrap();
        fs::create_dir(&public).unwrap();
        fs::write(public.join("foreign"), b"replacement").unwrap();
        let replacement = fs::metadata(&public).unwrap();
        assert!(prepare(f.root.as_raw_fd(), 102).is_err());
        assert_eq!(
            (
                fs::metadata(&public).unwrap().dev(),
                fs::metadata(&public).unwrap().ino()
            ),
            (replacement.dev(), replacement.ino())
        );

        let other = Fixture::new_unlocked();
        prepare(other.root.as_raw_fd(), 110).unwrap();
        let state = other.path.join(".darling-var-run-state-v1");
        fs::rename(&state, other.path.join("state-retained")).unwrap();
        fs::write(&state, b"foreign").unwrap();
        let replacement = fs::metadata(&state).unwrap();
        assert!(prepare(other.root.as_raw_fd(), 111).is_err());
        assert_eq!(
            (
                fs::metadata(&state).unwrap().dev(),
                fs::metadata(&state).unwrap().ino()
            ),
            (replacement.dev(), replacement.ino())
        );
    }

    #[test]
    fn torn_wal_tail_is_repaired_before_next_generation() {
        let f = Fixture::new();
        prepare(f.root.as_raw_fd(), 4).unwrap();
        fs::OpenOptions::new()
            .append(true)
            .open(f.path.join(".darling-var-run-state-v1"))
            .unwrap()
            .write_all(b"{torn")
            .unwrap();
        use std::io::Write;
        let out = prepare(f.root.as_raw_fd(), 5).unwrap();
        assert_eq!(out.generation, 5);
    }

    #[test]
    fn state_schema_is_exact_and_downgrade_fails_closed() {
        use std::io::Write;

        let f = Fixture::new();
        prepare(f.root.as_raw_fd(), 6).unwrap();
        let state = f.path.join(".darling-var-run-state-v1");
        let active = fs::read_to_string(&state).unwrap();
        let last = active.lines().last().unwrap();
        assert!(last.contains("\"schema_version\":3"));
        let downgraded = last.replacen("\"schema_version\":3", "\"schema_version\":2", 1);
        fs::OpenOptions::new()
            .append(true)
            .open(&state)
            .unwrap()
            .write_all(format!("{downgraded}\n").as_bytes())
            .unwrap();
        let public = fs::metadata(f.run()).unwrap();
        let error = prepare(f.root.as_raw_fd(), 7).unwrap_err();
        assert!(!error.recovery_required());
        let after = fs::metadata(f.run()).unwrap();
        assert_eq!((public.dev(), public.ino()), (after.dev(), after.ino()));
    }

    #[test]
    fn every_mutation_checkpoint_recovers_idempotently() {
        for point in (1..=8).chain([20, 21]) {
            let f = Fixture::new();
            fs::create_dir(f.run()).unwrap();
            fs::write(f.run().join("legacy"), b"preserve").unwrap();
            if (7..20).contains(&point) {
                prepare(f.root.as_raw_fd(), 40).unwrap();
                fs::write(f.run().join("owned"), b"collect").unwrap();
            }
            inject_fault(point);
            let generation = if (7..20).contains(&point) { 41 } else { 40 };
            let error = match prepare(f.root.as_raw_fd(), generation) {
                Err(error) => error,
                Ok(_) => panic!("checkpoint {point} was not reached"),
            };
            assert!(error.recovery_required(), "point {point}");
            let out = prepare(f.root.as_raw_fd(), generation).unwrap();
            assert_eq!(out.generation, generation);
            let m = fs::metadata(f.run()).unwrap();
            assert_eq!((m.dev(), m.ino()), out.identity);
            assert_eq!(
                prepare(f.root.as_raw_fd(), generation).unwrap().identity,
                out.identity
            );
            assert!(!f.path.join("var/tmp").exists());
        }
    }

    fn fork_prepare(fd: RawFd, generation: u64, fault: u8) -> i32 {
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            if fault != 0 {
                inject_fault(fault);
            }
            let ok = if fault == 0 {
                prepare(fd, generation).is_ok()
            } else {
                prepare(fd, generation).is_err()
            };
            unsafe { libc::_exit(i32::from(!ok)) }
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
        libc::WEXITSTATUS(status)
    }

    fn fork_expect_recovery(fd: RawFd, generation: u64) -> i32 {
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            let recovered = prepare(fd, generation).is_err_and(|error| error.recovery_required());
            unsafe { libc::_exit(i32::from(!recovered)) }
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
        libc::WEXITSTATUS(status)
    }

    fn fill_to_compaction(wal: &mut Wal, state: &State) {
        let mut record = serde_json::to_vec(state).unwrap();
        record.push(b'\n');
        while wal.len + record.len() <= MAX_WAL {
            write_all(wal.fd.as_raw_fd(), &record, "pad test WAL").unwrap();
            wal.len += record.len();
        }
        assert!(wal.len + record.len() > MAX_WAL);
        assert_eq!(unsafe { libc::fsync(wal.fd.as_raw_fd()) }, 0);
    }

    fn fork_compaction(fd: RawFd, point: u8) -> i32 {
        let pid = unsafe { libc::fork() };
        assert!(pid >= 0);
        if pid == 0 {
            let root = id(fd).unwrap();
            let var = acquire_var_parent(fd).unwrap();
            let (mut wal, state) = Wal::open(fd, var.as_raw_fd(), root).unwrap();
            let state = state.unwrap();
            fill_to_compaction(&mut wal, &state);
            inject_fault(point);
            let failed = wal.put(&state, true).is_err();
            unsafe { libc::_exit(i32::from(!failed)) }
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(pid, &mut status, 0) }, pid);
        libc::WEXITSTATUS(status)
    }

    #[test]
    fn fresh_process_recovers_after_namespace_mutations() {
        for point in [2, 4, 6, 8] {
            let f = Fixture::new();
            fs::create_dir(f.run()).unwrap();
            fs::write(f.run().join("legacy"), b"retained").unwrap();
            if point == 8 {
                assert_eq!(fork_prepare(f.root.as_raw_fd(), 70, 0), 0);
                fs::write(f.run().join("old"), b"collect").unwrap();
            }
            let generation = if point == 8 { 71 } else { 70 };
            assert_eq!(
                fork_prepare(f.root.as_raw_fd(), generation, point),
                0,
                "fault {point}"
            );
            assert_eq!(
                fork_prepare(f.root.as_raw_fd(), generation, 0),
                0,
                "recovery {point}"
            );
            let out = prepare(f.root.as_raw_fd(), generation).unwrap();
            let m = fs::metadata(f.run()).unwrap();
            assert_eq!((m.dev(), m.ino()), out.identity);
        }
    }

    #[test]
    fn bounded_compaction_survives_every_checkpoint_and_fresh_restart() {
        for point in 30..=35 {
            let f = Fixture::new();
            prepare(f.root.as_raw_fd(), 1).unwrap();
            assert_eq!(
                fork_compaction(f.root.as_raw_fd(), point),
                0,
                "point {point}"
            );
            assert_eq!(fork_prepare(f.root.as_raw_fd(), 2, 0), 0, "restart {point}");
            let out = prepare(f.root.as_raw_fd(), 2).unwrap();
            assert_eq!(out.generation, 2);
            let total = [STATE, SNAPSHOT_A, SNAPSHOT_B]
                .into_iter()
                .map(|name| {
                    fs::metadata(f.path.join(name.to_string_lossy().as_ref()))
                        .unwrap()
                        .len() as usize
                })
                .sum::<usize>();
            assert!(total <= MAX_WAL + 2 * MAX_SNAPSHOT);
        }
    }

    #[test]
    fn state_file_names_are_durable_before_first_namespace_mutation() {
        for point in 40..=53 {
            let f = Fixture::new();
            assert_eq!(
                fork_prepare(f.root.as_raw_fd(), 1, point),
                0,
                "point {point}"
            );
            assert!(!f.run().exists(), "point {point} mutated public var/run");
            assert_eq!(fork_prepare(f.root.as_raw_fd(), 1, 0), 0, "restart {point}");
            for name in [STATE, SNAPSHOT_A, SNAPSHOT_B] {
                let metadata =
                    fs::symlink_metadata(f.path.join(name.to_string_lossy().as_ref())).unwrap();
                assert!(metadata.file_type().is_file());
                assert_eq!(metadata.mode() & 0o777, 0o600);
                assert_eq!(metadata.nlink(), 1);
            }
        }
    }

    #[test]
    fn generations_continue_beyond_the_old_wal_budget_with_bounded_storage() {
        let f = Fixture::new();
        fs::create_dir(f.run()).unwrap();
        fs::write(f.run().join("legacy"), b"forensic").unwrap();
        let first = prepare(f.root.as_raw_fd(), 1).unwrap();
        let forensic = first.forensic_quarantine.unwrap();
        let forensic_before = fs::metadata(f.path.join("var").join(&forensic)).unwrap();
        for generation in 2..=96 {
            prepare(f.root.as_raw_fd(), generation).unwrap();
        }
        assert!(
            fs::metadata(f.path.join(SNAPSHOT_A.to_string_lossy().as_ref()))
                .unwrap()
                .len()
                > 0
                || fs::metadata(f.path.join(SNAPSHOT_B.to_string_lossy().as_ref()))
                    .unwrap()
                    .len()
                    > 0
        );
        assert_eq!(fork_prepare(f.root.as_raw_fd(), 97, 0), 0);
        let forensic_after = fs::metadata(f.path.join("var").join(&forensic)).unwrap();
        assert_eq!(
            (forensic_before.dev(), forensic_before.ino()),
            (forensic_after.dev(), forensic_after.ino())
        );
        let total = [STATE, SNAPSHOT_A, SNAPSHOT_B]
            .into_iter()
            .map(|name| {
                fs::metadata(f.path.join(name.to_string_lossy().as_ref()))
                    .unwrap()
                    .len() as usize
            })
            .sum::<usize>();
        assert!(total <= MAX_WAL + 2 * MAX_SNAPSHOT);
    }

    #[test]
    fn forensic_missing_and_same_metadata_replacements_are_never_collected() {
        for missing in [true, false] {
            let f = Fixture::new();
            fs::create_dir(f.run()).unwrap();
            fs::write(f.run().join("legacy"), b"original").unwrap();
            let out = prepare(f.root.as_raw_fd(), 1).unwrap();
            let name = out.forensic_quarantine.unwrap();
            let public_before = fs::metadata(f.run()).unwrap();
            let recorded = f.path.join("var").join(&name);
            let displaced = f.path.join("var/forensic-displaced");
            fs::rename(&recorded, &displaced).unwrap();
            let original = fs::metadata(&displaced).unwrap();
            if !missing {
                fs::create_dir(&recorded).unwrap();
                fs::write(recorded.join("replacement"), b"byte-identical-check").unwrap();
                fs::set_permissions(
                    &recorded,
                    fs::Permissions::from_mode(original.mode() & 0o7777),
                )
                .unwrap();
            }
            let error = prepare(f.root.as_raw_fd(), 2).unwrap_err();
            assert!(error.recovery_required());
            let public_after = fs::metadata(f.run()).unwrap();
            assert_eq!(
                (public_before.dev(), public_before.ino()),
                (public_after.dev(), public_after.ino())
            );
            let preserved = fs::metadata(&displaced).unwrap();
            assert_eq!(
                (original.dev(), original.ino()),
                (preserved.dev(), preserved.ino())
            );
            assert_eq!(fs::read(displaced.join("legacy")).unwrap(), b"original");
            if !missing {
                assert_eq!(
                    fs::read(recorded.join("replacement")).unwrap(),
                    b"byte-identical-check"
                );
            }
        }
    }

    #[test]
    fn legacy_string_forensic_records_remain_unbound_across_replacement() {
        use std::io::Write;

        for replace in [false, true] {
            let f = Fixture::new();
            fs::create_dir(f.run()).unwrap();
            fs::write(f.run().join("legacy"), b"same-bytes").unwrap();
            let first = prepare(f.root.as_raw_fd(), 1).unwrap();
            let name = first.forensic_quarantine.unwrap();
            let recorded = f.path.join("var").join(&name);
            let displaced = f.path.join("var/legacy-unbound-displaced");
            let state_path = f.path.join(STATE.to_string_lossy().as_ref());
            let text = fs::read_to_string(&state_path).unwrap();
            let mut legacy: serde_json::Value =
                serde_json::from_str(text.lines().last().unwrap()).unwrap();
            legacy["forensic"] = serde_json::json!([name]);
            fs::OpenOptions::new()
                .append(true)
                .open(&state_path)
                .unwrap()
                .write_all(format!("{legacy}\n").as_bytes())
                .unwrap();
            let wal_before = fs::metadata(&state_path).unwrap().len();
            let public_before = fs::metadata(f.run()).unwrap();
            if replace {
                fs::rename(&recorded, &displaced).unwrap();
                let original = fs::metadata(&displaced).unwrap();
                fs::create_dir(&recorded).unwrap();
                fs::write(recorded.join("legacy"), b"same-bytes").unwrap();
                fs::set_permissions(
                    &recorded,
                    fs::Permissions::from_mode(original.mode() & 0o7777),
                )
                .unwrap();
            }
            assert_eq!(fork_expect_recovery(f.root.as_raw_fd(), 2), 0);
            assert_eq!(fs::metadata(&state_path).unwrap().len(), wal_before);
            let public_after = fs::metadata(f.run()).unwrap();
            assert_eq!(
                (public_before.dev(), public_before.ino()),
                (public_after.dev(), public_after.ino())
            );
            assert_eq!(fs::read(recorded.join("legacy")).unwrap(), b"same-bytes");
            if replace {
                assert_eq!(fs::read(displaced.join("legacy")).unwrap(), b"same-bytes");
                assert_ne!(
                    fs::metadata(&recorded).unwrap().ino(),
                    fs::metadata(&displaced).unwrap().ino()
                );
            }
        }
    }
}
