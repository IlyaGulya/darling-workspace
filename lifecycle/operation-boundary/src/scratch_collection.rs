//! Final, lease-bound collection of one retained owned-scratch root.

#![deny(unsafe_code)]

use crate::scratch_fs::{
    chmod, create_exclusive, entries, fstat, lock_exclusive_nonblocking, named_stat,
    open_directory, open_path, open_rw, parent_pid, remove_directory, rename_noreplace, sync,
    unlink, unlock,
};
use crate::scratch_process_census::{
    census, live_process_starttime, CensusOutcome, CensusRequest, IgnoredDescriptor,
};
use serde::{Deserialize, Serialize};
use std::ffi::{CStr, CString, OsString};
use std::fs::File;
use std::io::{self, Read, Write};
use std::os::fd::{AsFd, AsRawFd, BorrowedFd, OwnedFd, RawFd};
use std::os::unix::ffi::OsStringExt;
use std::os::unix::fs::FileExt;
use std::time::{Duration, Instant};

pub const COLLECTION_PROTOCOL_VERSION: u32 = 1;
const MARKER: &[u8] = b".darling-scratch-v1";
const LEASE: &[u8] = b".darling-scratch-lease";
const MAX_ENTRIES: usize = 1_000_000;
const MAX_DEPTH: usize = 256;
const MAX_TIME_MS: u64 = 60_000;
const MAX_MOUNTINFO_BYTES: usize = 8 * 1024 * 1024;
const MAX_AUTHORITY_BYTES: usize = 4096;
const AUTHORITY_SUFFIX: &str = ".authority";

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FileIdentity {
    pub device: u64,
    pub inode: u64,
    pub file_type: u32,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
    pub links: u64,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ImmutableIdentity {
    pub device: u64,
    pub inode: u64,
    pub file_type: u32,
    pub mode: u32,
    pub uid: u32,
    pub gid: u32,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CollectionRequest {
    pub protocol_version: u32,
    pub operation: CollectionOperation,
    pub namespace_fd: i32,
    pub root_fd: i32,
    pub marker_fd: i32,
    pub lease_fd: i32,
    pub root_name: String,
    pub quarantine_name: String,
    pub authority_name: String,
    pub namespace_identity: FileIdentity,
    pub root_identity: FileIdentity,
    pub marker_identity: FileIdentity,
    pub lease_identity: FileIdentity,
    pub process_limit: usize,
    pub fd_limit: usize,
    pub entry_limit: usize,
    pub depth_limit: usize,
    pub time_limit_ms: u64,
    pub output_limit_bytes: usize,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct RecoveryRequest {
    pub protocol_version: u32,
    pub operation: RecoveryOperation,
    pub namespace_fd: i32,
    pub authority_fd: i32,
    pub authority_name: String,
    pub namespace_identity: FileIdentity,
    pub authority_identity: FileIdentity,
    pub process_limit: usize,
    pub fd_limit: usize,
    pub entry_limit: usize,
    pub depth_limit: usize,
    pub time_limit_ms: u64,
    pub output_limit_bytes: usize,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryOperation {
    Recover,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CollectionOperation {
    Collect,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RetainReason {
    IdentityMismatch,
    ActiveReference,
    AmbiguousCensus,
    MountedSubtree,
    BudgetExceeded,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryReason {
    DeleteFailed,
    BudgetExceeded,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(tag = "outcome", rename_all = "snake_case")]
pub enum CollectionOutcome {
    Collected {
        entries: usize,
    },
    Retained {
        reason: RetainReason,
    },
    Quarantined {
        name: String,
        authority_name: String,
        identity: ImmutableIdentity,
        observed_links: u64,
        reason: RecoveryReason,
    },
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct AuthorityRecord {
    version: u32,
    public_name: String,
    quarantine_name: String,
    root_identity: ImmutableIdentity,
    root_links: u64,
    marker_identity: FileIdentity,
    lease_identity: FileIdentity,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct CollectionResponse {
    pub protocol_version: u32,
    pub result: CollectionOutcome,
}

#[derive(Debug)]
pub enum CollectionError {
    InvalidRequest(&'static str),
    Io(io::Error),
}

impl std::fmt::Display for CollectionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidRequest(message) => write!(f, "invalid collection request: {message}"),
            Self::Io(error) => write!(f, "collection transport error: {error}"),
        }
    }
}

impl std::error::Error for CollectionError {}

impl From<io::Error> for CollectionError {
    fn from(value: io::Error) -> Self {
        Self::Io(value)
    }
}

struct Capabilities {
    namespace: OwnedFd,
    root: OwnedFd,
    marker: OwnedFd,
    lease: OwnedFd,
}

struct DeleteBudget {
    deadline: Instant,
    entries: usize,
    entry_limit: usize,
    depth_limit: usize,
}

impl DeleteBudget {
    fn charge(&mut self, depth: usize) -> io::Result<()> {
        if depth > self.depth_limit
            || self.entries >= self.entry_limit
            || Instant::now() >= self.deadline
        {
            return Err(io::Error::from_raw_os_error(libc::EFBIG));
        }
        self.entries += 1;
        Ok(())
    }
}

fn duplicate(raw: RawFd) -> Result<OwnedFd, CollectionError> {
    crate::scratch_process_census::duplicate_inherited_root(raw)
        .map_err(|_| CollectionError::InvalidRequest("invalid inherited descriptor"))
}

fn identity(value: &rustix::fs::Stat) -> FileIdentity {
    FileIdentity {
        device: value.st_dev,
        inode: value.st_ino,
        file_type: value.st_mode & libc::S_IFMT,
        mode: value.st_mode & 0o7777,
        uid: value.st_uid,
        gid: value.st_gid,
        links: value.st_nlink,
    }
}

fn immutable(value: &FileIdentity) -> ImmutableIdentity {
    ImmutableIdentity {
        device: value.device,
        inode: value.inode,
        file_type: value.file_type,
        mode: value.mode,
        uid: value.uid,
        gid: value.gid,
    }
}

fn same_object(left: &rustix::fs::Stat, right: &rustix::fs::Stat) -> bool {
    left.st_dev == right.st_dev && left.st_ino == right.st_ino
}

fn authority_matches(actual: &FileIdentity, expected: &FileIdentity) -> bool {
    actual.device == expected.device
        && actual.inode == expected.inode
        && actual.file_type == expected.file_type
        && actual.mode == expected.mode
        && actual.uid == expected.uid
        && actual.gid == expected.gid
        && (actual.file_type == libc::S_IFDIR || actual.links == expected.links)
}

fn component(value: &str) -> Result<CString, CollectionError> {
    if value.is_empty() || value == "." || value == ".." || value.as_bytes().contains(&b'/') {
        return Err(CollectionError::InvalidRequest(
            "name is not one path component",
        ));
    }
    CString::new(value).map_err(|_| CollectionError::InvalidRequest("name contains NUL"))
}

fn quarantine_uuid(value: &str) -> Option<&str> {
    let suffix = value.strip_prefix(".gc-")?;
    (suffix.len() == 32
        && suffix
            .as_bytes()
            .iter()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(byte)))
    .then_some(suffix)
}

fn validate_collection_names(request: &CollectionRequest) -> Result<(), CollectionError> {
    component(&request.root_name)?;
    if request.root_name.starts_with(".gc-") {
        return Err(CollectionError::InvalidRequest("reserved public root name"));
    }
    quarantine_uuid(&request.quarantine_name)
        .ok_or(CollectionError::InvalidRequest("invalid quarantine name"))?;
    if request.authority_name != format!("{}{}", request.quarantine_name, AUTHORITY_SUFFIX) {
        return Err(CollectionError::InvalidRequest("invalid authority name"));
    }
    component(&request.authority_name)?;
    Ok(())
}

fn validate_recovery_name(request: &RecoveryRequest) -> Result<String, CollectionError> {
    let quarantine_name = request
        .authority_name
        .strip_suffix(AUTHORITY_SUFFIX)
        .ok_or(CollectionError::InvalidRequest(
            "invalid recovery authority name",
        ))?;
    quarantine_uuid(quarantine_name).ok_or(CollectionError::InvalidRequest(
        "invalid recovery quarantine name",
    ))?;
    component(&request.authority_name)?;
    Ok(quarantine_name.to_owned())
}

fn validate_capabilities(
    request: &CollectionRequest,
    caps: &Capabilities,
) -> Result<bool, CollectionError> {
    let expected = [
        (&request.namespace_identity, &caps.namespace),
        (&request.root_identity, &caps.root),
        (&request.marker_identity, &caps.marker),
        (&request.lease_identity, &caps.lease),
    ];
    for (wanted, fd) in expected {
        if !authority_matches(&identity(&fstat(fd.as_fd())?), wanted) {
            return Ok(false);
        }
    }
    let root_name = component(&request.root_name)?;
    if !authority_matches(
        &identity(&named_stat(caps.namespace.as_fd(), &root_name)?),
        &request.root_identity,
    ) {
        return Ok(false);
    }
    for (name, wanted) in [
        (MARKER, &request.marker_identity),
        (LEASE, &request.lease_identity),
    ] {
        let name = CString::new(name)
            .map_err(|_| CollectionError::InvalidRequest("invalid anchor name"))?;
        let matches = named_stat(caps.root.as_fd(), &name)
            .map(|value| identity(&value) == *wanted)
            .unwrap_or(false);
        if !matches {
            return Ok(false);
        }
    }
    Ok(true)
}

fn mounted_inside(root: BorrowedFd<'_>, deadline: Instant) -> io::Result<bool> {
    let target = std::fs::read_link(format!("/proc/self/fd/{}", root.as_raw_fd()))?;
    let payload = read_mountinfo_bounded(std::fs::File::open("/proc/self/mountinfo")?)?;
    mountinfo_contains(&payload, &target, deadline)
}

fn read_mountinfo_bounded(reader: impl Read) -> io::Result<Vec<u8>> {
    let mut payload = Vec::with_capacity(MAX_MOUNTINFO_BYTES.min(64 * 1024));
    reader
        .take((MAX_MOUNTINFO_BYTES + 1) as u64)
        .read_to_end(&mut payload)?;
    if payload.len() > MAX_MOUNTINFO_BYTES {
        return Err(io::Error::from_raw_os_error(libc::EFBIG));
    }
    Ok(payload)
}

fn mountinfo_contains(
    payload: &[u8],
    target: &std::path::Path,
    deadline: Instant,
) -> io::Result<bool> {
    for line in payload.split(|byte| *byte == b'\n') {
        if line.is_empty() {
            continue;
        }
        if Instant::now() >= deadline {
            return Err(io::Error::from_raw_os_error(libc::ETIMEDOUT));
        }
        let fields: Vec<&[u8]> = line.split(|byte| *byte == b' ').collect();
        if fields.len() < 5 {
            return Ok(true);
        }
        let mut decoded = Vec::with_capacity(fields[4].len());
        let mut offset = 0;
        while offset < fields[4].len() {
            let remaining = &fields[4][offset..];
            let escaped = [
                (b"\\040".as_slice(), b' '),
                (b"\\011".as_slice(), b'\t'),
                (b"\\012".as_slice(), b'\n'),
                (b"\\134".as_slice(), b'\\'),
            ]
            .into_iter()
            .find(|(escape, _)| remaining.starts_with(escape));
            if let Some((escape, value)) = escaped {
                decoded.push(value);
                offset += escape.len();
            } else {
                decoded.push(fields[4][offset]);
                offset += 1;
            }
        }
        let mount = OsString::from_vec(decoded);
        let mount = std::path::PathBuf::from(mount);
        if mount == target || mount.starts_with(target) {
            return Ok(true);
        }
    }
    Ok(false)
}

fn remove_contents(
    directory: BorrowedFd<'_>,
    budget: &mut DeleteBudget,
    depth: usize,
) -> io::Result<()> {
    let remaining = budget.entry_limit.saturating_sub(budget.entries);
    for name in entries(directory, remaining, budget.deadline)? {
        budget.charge(depth)?;
        if name.to_bytes() == MARKER || name.to_bytes() == LEASE {
            continue;
        }
        let before = named_stat(directory, &name)?;
        if before.st_mode & libc::S_IFMT == libc::S_IFDIR {
            let child = open_directory(directory, &name)?;
            if !same_object(&fstat(child.as_fd())?, &before) {
                return Err(io::Error::from_raw_os_error(libc::ESTALE));
            }
            remove_contents(child.as_fd(), budget, depth + 1)?;
            if !same_object(&named_stat(directory, &name)?, &before) {
                return Err(io::Error::from_raw_os_error(libc::ESTALE));
            }
            remove_directory(directory, &name)?;
        } else {
            if identity(&named_stat(directory, &name)?) != identity(&before) {
                return Err(io::Error::from_raw_os_error(libc::ESTALE));
            }
            unlink(directory, &name)?;
        }
    }
    Ok(())
}

fn create_authority(
    namespace: BorrowedFd<'_>,
    name: &CStr,
    record: &AuthorityRecord,
) -> io::Result<(OwnedFd, FileIdentity)> {
    let authority = create_exclusive(namespace, name, rustix::fs::Mode::from_raw_mode(0o600))?;
    chmod(authority.as_fd(), rustix::fs::Mode::from_raw_mode(0o600))?;
    let payload = serde_json::to_vec(record).map_err(io::Error::other)?;
    if payload.len() + 2 > MAX_AUTHORITY_BYTES {
        return Err(io::Error::from_raw_os_error(libc::EFBIG));
    }
    let mut encoded = Vec::with_capacity(payload.len() + 2);
    encoded.extend_from_slice(b"P\n");
    encoded.extend_from_slice(&payload);
    let mut file = File::from(rustix::io::dup(authority.as_fd()).map_err(io::Error::from)?);
    file.write_all(&encoded)?;
    file.sync_all()?;
    sync(namespace)?;
    let opened = identity(&fstat(authority.as_fd())?);
    let named = identity(&named_stat(namespace, name)?);
    if opened != named {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    Ok((authority, opened))
}

fn set_phase(authority: BorrowedFd<'_>, phase: u8) -> io::Result<()> {
    if !matches!(phase, b'Q' | b'M' | b'L' | b'R') {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    let file = File::from(rustix::io::dup(authority).map_err(io::Error::from)?);
    if file.write_at(&[phase], 0)? != 1 {
        return Err(io::Error::from_raw_os_error(libc::EIO));
    }
    file.sync_all()
}

fn read_authority(authority: BorrowedFd<'_>) -> io::Result<(u8, AuthorityRecord)> {
    let mut payload = vec![0_u8; MAX_AUTHORITY_BYTES + 1];
    let file = File::from(rustix::io::dup(authority).map_err(io::Error::from)?);
    let mut count = 0;
    while count < payload.len() {
        let read = file.read_at(&mut payload[count..], count as u64)?;
        if read == 0 {
            break;
        }
        count += read;
    }
    if !(3..=MAX_AUTHORITY_BYTES).contains(&count) || payload.get(1) != Some(&b'\n') {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    let phase = payload[0];
    if !matches!(phase, b'P' | b'Q' | b'M' | b'L' | b'R') {
        return Err(io::Error::from_raw_os_error(libc::EINVAL));
    }
    let record = serde_json::from_slice(&payload[2..count])
        .map_err(|_| io::Error::from_raw_os_error(libc::EINVAL))?;
    Ok((phase, record))
}

fn remove_exact(parent: BorrowedFd<'_>, name: &CStr, expected: &FileIdentity) -> io::Result<()> {
    if identity(&named_stat(parent, name)?) != *expected {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    unlink(parent, name)
}

fn acquire_exclusive_lease(root: BorrowedFd<'_>, retained: BorrowedFd<'_>) -> io::Result<()> {
    lock_exclusive_nonblocking(retained)?;
    let name = CString::new(LEASE).map_err(io::Error::other)?;
    let contender = open_rw(root, &name)?;
    if !same_object(&fstat(contender.as_fd())?, &fstat(retained)?) {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    match lock_exclusive_nonblocking(contender.as_fd()) {
        Ok(()) => {
            unlock(contender.as_fd())?;
            return Err(io::Error::from_raw_os_error(libc::ENOLCK));
        }
        Err(error) if error.raw_os_error() == Some(libc::EWOULDBLOCK) => {}
        Err(error) => return Err(error),
    }
    Ok(())
}

fn validate_budgets(
    protocol_version: u32,
    entry_limit: usize,
    depth_limit: usize,
    time_limit_ms: u64,
) -> Result<(), CollectionError> {
    if protocol_version != COLLECTION_PROTOCOL_VERSION
        || entry_limit == 0
        || entry_limit > MAX_ENTRIES
        || depth_limit == 0
        || depth_limit > MAX_DEPTH
        || time_limit_ms == 0
        || time_limit_ms > MAX_TIME_MS
    {
        return Err(CollectionError::InvalidRequest(
            "unsupported protocol or budget",
        ));
    }
    Ok(())
}

fn retained(reason: RetainReason) -> CollectionResponse {
    CollectionResponse {
        protocol_version: COLLECTION_PROTOCOL_VERSION,
        result: CollectionOutcome::Retained { reason },
    }
}

fn recovery_pending(
    name: &str,
    authority_name: &str,
    root: BorrowedFd<'_>,
    reason: RecoveryReason,
) -> CollectionResponse {
    let current = fstat(root).map(|value| identity(&value));
    let (identity, observed_links) = current
        .map(|value| (immutable(&value), value.links))
        .unwrap_or((
            ImmutableIdentity {
                device: 0,
                inode: 0,
                file_type: 0,
                mode: 0,
                uid: 0,
                gid: 0,
            },
            0,
        ));
    CollectionResponse {
        protocol_version: COLLECTION_PROTOCOL_VERSION,
        result: CollectionOutcome::Quarantined {
            name: name.to_owned(),
            authority_name: authority_name.to_owned(),
            identity,
            observed_links,
            reason,
        },
    }
}

fn transport_ignores(
    raw: &[RawFd],
    duplicated: &[RawFd],
) -> Result<Vec<IgnoredDescriptor>, CollectionError> {
    let helper_pid = std::process::id();
    let helper_starttime = live_process_starttime(helper_pid)?;
    let mut ignored = Vec::with_capacity(raw.len() + duplicated.len());
    if let Some(pid) = parent_pid() {
        let starttime = live_process_starttime(pid)?;
        ignored.extend(raw.iter().filter_map(|fd| {
            u32::try_from(*fd)
                .ok()
                .map(|fd| IgnoredDescriptor { pid, starttime, fd })
        }));
    }
    ignored.extend(duplicated.iter().filter_map(|fd| {
        u32::try_from(*fd).ok().map(|fd| IgnoredDescriptor {
            pid: helper_pid,
            starttime: helper_starttime,
            fd,
        })
    }));
    Ok(ignored)
}

fn safe_to_delete(
    root: BorrowedFd<'_>,
    process_limit: usize,
    fd_limit: usize,
    time_limit_ms: u64,
    output_limit_bytes: usize,
    ignored_descriptors: Vec<IgnoredDescriptor>,
    deadline: Instant,
) -> Result<Option<RetainReason>, CollectionError> {
    let census_request = CensusRequest {
        protocol_version: crate::scratch_process_census::PROTOCOL_VERSION,
        root_fd: root.as_raw_fd(),
        process_limit,
        fd_limit,
        time_limit_ms,
        output_limit_bytes,
        ignored_descriptors,
    };
    let census_response = census(duplicate(root.as_raw_fd())?, &census_request)
        .map_err(|_| CollectionError::InvalidRequest("invalid census budget"))?;
    if census_response
        .outcomes
        .iter()
        .any(|item| matches!(item, CensusOutcome::Reference { .. }))
    {
        return Ok(Some(RetainReason::ActiveReference));
    }
    if census_response.outcomes.iter().any(|item| {
        !matches!(
            item,
            CensusOutcome::Unreadable {
                errno: libc::EACCES | libc::EPERM,
                ..
            }
        )
    }) {
        return Ok(Some(RetainReason::AmbiguousCensus));
    }
    match mounted_inside(root, deadline) {
        Ok(true) => Ok(Some(RetainReason::MountedSubtree)),
        Err(error) if matches!(error.raw_os_error(), Some(libc::EFBIG | libc::ETIMEDOUT)) => {
            Ok(Some(RetainReason::BudgetExceeded))
        }
        Err(error) => Err(error.into()),
        Ok(false) => Ok(None),
    }
}

fn anchor_state(
    root: BorrowedFd<'_>,
    name: &[u8],
    expected: &FileIdentity,
) -> io::Result<Option<CString>> {
    let name = CString::new(name).map_err(io::Error::other)?;
    match named_stat(root, &name) {
        Ok(value) if identity(&value) == *expected => Ok(Some(name)),
        Err(error) if error.raw_os_error() == Some(libc::ENOENT) => Ok(None),
        _ => Err(io::Error::from_raw_os_error(libc::ESTALE)),
    }
}

struct DeleteAuthority<'a> {
    namespace: BorrowedFd<'a>,
    root: BorrowedFd<'a>,
    marker: Option<BorrowedFd<'a>>,
    lease: Option<BorrowedFd<'a>>,
    quarantine: &'a CStr,
    sidecar: BorrowedFd<'a>,
    sidecar_name: &'a CStr,
    sidecar_identity: &'a FileIdentity,
    record: &'a AuthorityRecord,
}

fn finish_delete(
    authority: &DeleteAuthority<'_>,
    budget: &mut DeleteBudget,
    mut phase: u8,
) -> io::Result<()> {
    if phase == b'Q' {
        remove_contents(authority.root, budget, 0)?;
        set_phase(authority.sidecar, b'M')?;
        phase = b'M';
    }
    if phase == b'M' {
        if let Some(name) = anchor_state(authority.root, MARKER, &authority.record.marker_identity)?
        {
            let retained = authority
                .marker
                .ok_or_else(|| io::Error::from_raw_os_error(libc::ESTALE))?;
            if identity(&fstat(retained)?) != authority.record.marker_identity {
                return Err(io::Error::from_raw_os_error(libc::ESTALE));
            }
            remove_exact(authority.root, &name, &authority.record.marker_identity)?;
        }
        set_phase(authority.sidecar, b'L')?;
        phase = b'L';
    }
    if phase == b'L' {
        set_phase(authority.sidecar, b'R')?;
        phase = b'R';
    }
    if phase == b'R' {
        if let Some(name) = anchor_state(authority.root, LEASE, &authority.record.lease_identity)? {
            let retained = authority
                .lease
                .ok_or_else(|| io::Error::from_raw_os_error(libc::ESTALE))?;
            if identity(&fstat(retained)?) != authority.record.lease_identity {
                return Err(io::Error::from_raw_os_error(libc::ESTALE));
            }
            remove_exact(authority.root, &name, &authority.record.lease_identity)?;
        }
    }
    let opened = identity(&fstat(authority.root)?);
    if immutable(&opened) != authority.record.root_identity {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    let named = identity(&named_stat(authority.namespace, authority.quarantine)?);
    if immutable(&named) != authority.record.root_identity
        || !same_object(
            &fstat(authority.root)?,
            &named_stat(authority.namespace, authority.quarantine)?,
        )
    {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    remove_directory(authority.namespace, authority.quarantine)?;
    sync(authority.namespace)?;
    if identity(&fstat(authority.sidecar)?) != *authority.sidecar_identity {
        return Err(io::Error::from_raw_os_error(libc::ESTALE));
    }
    remove_exact(
        authority.namespace,
        authority.sidecar_name,
        authority.sidecar_identity,
    )?;
    sync(authority.namespace)
}

pub fn collect(request: &CollectionRequest) -> Result<CollectionResponse, CollectionError> {
    validate_budgets(
        request.protocol_version,
        request.entry_limit,
        request.depth_limit,
        request.time_limit_ms,
    )?;
    validate_collection_names(request)?;
    let caps = Capabilities {
        namespace: duplicate(request.namespace_fd)?,
        root: duplicate(request.root_fd)?,
        marker: duplicate(request.marker_fd)?,
        lease: duplicate(request.lease_fd)?,
    };
    if !validate_capabilities(request, &caps)? {
        return Ok(retained(RetainReason::IdentityMismatch));
    }
    if acquire_exclusive_lease(caps.root.as_fd(), caps.lease.as_fd()).is_err() {
        return Ok(retained(RetainReason::IdentityMismatch));
    }
    let deadline = Instant::now() + Duration::from_millis(request.time_limit_ms);
    let ignored = transport_ignores(
        &[request.root_fd, request.marker_fd, request.lease_fd],
        &[
            request.root_fd,
            request.marker_fd,
            request.lease_fd,
            caps.root.as_raw_fd(),
            caps.marker.as_raw_fd(),
            caps.lease.as_raw_fd(),
        ],
    )?;
    if let Some(reason) = safe_to_delete(
        caps.root.as_fd(),
        request.process_limit,
        request.fd_limit,
        request.time_limit_ms,
        request.output_limit_bytes,
        ignored,
        deadline,
    )? {
        return Ok(retained(reason));
    }
    let source = component(&request.root_name)?;
    let quarantine = component(&request.quarantine_name)?;
    let authority_name = component(&request.authority_name)?;
    let record = AuthorityRecord {
        version: COLLECTION_PROTOCOL_VERSION,
        public_name: request.root_name.clone(),
        quarantine_name: request.quarantine_name.clone(),
        root_identity: immutable(&request.root_identity),
        root_links: request.root_identity.links,
        marker_identity: request.marker_identity.clone(),
        lease_identity: request.lease_identity.clone(),
    };
    let (authority, authority_identity) =
        match create_authority(caps.namespace.as_fd(), &authority_name, &record) {
            Ok(value) => value,
            Err(_) => return Ok(retained(RetainReason::IdentityMismatch)),
        };
    if !validate_capabilities(request, &caps)? {
        return Ok(recovery_pending(
            &request.quarantine_name,
            &request.authority_name,
            caps.root.as_fd(),
            RecoveryReason::DeleteFailed,
        ));
    }
    if rename_noreplace(caps.namespace.as_fd(), &source, &quarantine).is_err() {
        if remove_exact(caps.namespace.as_fd(), &authority_name, &authority_identity)
            .and_then(|()| sync(caps.namespace.as_fd()))
            .is_ok()
        {
            return Ok(retained(RetainReason::IdentityMismatch));
        }
        return Ok(recovery_pending(
            &request.quarantine_name,
            &request.authority_name,
            caps.root.as_fd(),
            RecoveryReason::DeleteFailed,
        ));
    }
    if sync(caps.namespace.as_fd()).is_err()
        || named_stat(caps.namespace.as_fd(), &quarantine)
            .map(|value| immutable(&identity(&value)) != record.root_identity)
            .unwrap_or(true)
        || set_phase(authority.as_fd(), b'Q').is_err()
    {
        return Ok(recovery_pending(
            &request.quarantine_name,
            &request.authority_name,
            caps.root.as_fd(),
            RecoveryReason::DeleteFailed,
        ));
    }
    let mut budget = DeleteBudget {
        deadline,
        entries: 0,
        entry_limit: request.entry_limit,
        depth_limit: request.depth_limit,
    };
    let delete_authority = DeleteAuthority {
        namespace: caps.namespace.as_fd(),
        root: caps.root.as_fd(),
        marker: Some(caps.marker.as_fd()),
        lease: Some(caps.lease.as_fd()),
        quarantine: &quarantine,
        sidecar: authority.as_fd(),
        sidecar_name: &authority_name,
        sidecar_identity: &authority_identity,
        record: &record,
    };
    if let Err(error) = finish_delete(&delete_authority, &mut budget, b'Q') {
        let reason = if matches!(error.raw_os_error(), Some(libc::EFBIG | libc::ETIMEDOUT))
            || Instant::now() >= deadline
            || budget.entries >= budget.entry_limit
        {
            RecoveryReason::BudgetExceeded
        } else {
            RecoveryReason::DeleteFailed
        };
        return Ok(recovery_pending(
            &request.quarantine_name,
            &request.authority_name,
            caps.root.as_fd(),
            reason,
        ));
    }
    Ok(CollectionResponse {
        protocol_version: COLLECTION_PROTOCOL_VERSION,
        result: CollectionOutcome::Collected {
            entries: budget.entries,
        },
    })
}

pub fn recover(request: &RecoveryRequest) -> Result<CollectionResponse, CollectionError> {
    validate_budgets(
        request.protocol_version,
        request.entry_limit,
        request.depth_limit,
        request.time_limit_ms,
    )?;
    let quarantine_name = validate_recovery_name(request)?;
    let namespace = duplicate(request.namespace_fd)?;
    let authority = duplicate(request.authority_fd)?;
    if !authority_matches(
        &identity(&fstat(namespace.as_fd())?),
        &request.namespace_identity,
    ) || identity(&fstat(authority.as_fd())?) != request.authority_identity
    {
        return Ok(retained(RetainReason::IdentityMismatch));
    }
    let authority_name = component(&request.authority_name)?;
    if identity(&named_stat(namespace.as_fd(), &authority_name)?) != request.authority_identity {
        return Ok(retained(RetainReason::IdentityMismatch));
    }
    let (phase, record) = read_authority(authority.as_fd())?;
    if record.version != COLLECTION_PROTOCOL_VERSION || record.quarantine_name != quarantine_name {
        return Err(CollectionError::InvalidRequest("authority record mismatch"));
    }
    let quarantine = component(&quarantine_name)?;
    let public = component(&record.public_name)?;
    let root = match open_directory(namespace.as_fd(), &quarantine) {
        Ok(root) => root,
        Err(error) if error.raw_os_error() == Some(libc::ENOENT) => {
            if phase == b'P' {
                let public_exact = named_stat(namespace.as_fd(), &public)
                    .map(|value| immutable(&identity(&value)) == record.root_identity)
                    .unwrap_or(false);
                if !public_exact {
                    return Ok(retained(RetainReason::IdentityMismatch));
                }
            }
            remove_exact(
                namespace.as_fd(),
                &authority_name,
                &request.authority_identity,
            )?;
            sync(namespace.as_fd())?;
            return Ok(CollectionResponse {
                protocol_version: COLLECTION_PROTOCOL_VERSION,
                result: CollectionOutcome::Collected { entries: 0 },
            });
        }
        Err(_) => return Ok(retained(RetainReason::IdentityMismatch)),
    };
    let root_state = identity(&fstat(root.as_fd())?);
    if immutable(&root_state) != record.root_identity {
        return Ok(retained(RetainReason::IdentityMismatch));
    }
    let phase = if phase == b'P' {
        set_phase(authority.as_fd(), b'Q')?;
        b'Q'
    } else {
        phase
    };
    let lease_required = phase != b'R';
    let marker_required = phase == b'Q';
    let marker_name = CString::new(MARKER).map_err(io::Error::other)?;
    let lease_name = CString::new(LEASE).map_err(io::Error::other)?;
    let marker = match open_path(root.as_fd(), &marker_name) {
        Ok(marker) if identity(&fstat(marker.as_fd())?) == record.marker_identity => Some(marker),
        Err(error) if !marker_required && error.raw_os_error() == Some(libc::ENOENT) => None,
        _ => return Ok(retained(RetainReason::IdentityMismatch)),
    };
    let lease = match open_rw(root.as_fd(), &lease_name) {
        Ok(lease) if identity(&fstat(lease.as_fd())?) == record.lease_identity => Some(lease),
        Err(error) if !lease_required && error.raw_os_error() == Some(libc::ENOENT) => None,
        _ => return Ok(retained(RetainReason::IdentityMismatch)),
    };
    if let Some(lease) = lease.as_ref() {
        if acquire_exclusive_lease(root.as_fd(), lease.as_fd()).is_err() {
            return Ok(retained(RetainReason::IdentityMismatch));
        }
    }
    let deadline = Instant::now() + Duration::from_millis(request.time_limit_ms);
    let ignored = transport_ignores(
        &[],
        &[
            Some(root.as_raw_fd()),
            marker.as_ref().map(AsRawFd::as_raw_fd),
            lease.as_ref().map(AsRawFd::as_raw_fd),
        ]
        .into_iter()
        .flatten()
        .collect::<Vec<_>>(),
    )?;
    if let Some(reason) = safe_to_delete(
        root.as_fd(),
        request.process_limit,
        request.fd_limit,
        request.time_limit_ms,
        request.output_limit_bytes,
        ignored,
        deadline,
    )? {
        return Ok(retained(reason));
    }
    let mut budget = DeleteBudget {
        deadline,
        entries: 0,
        entry_limit: request.entry_limit,
        depth_limit: request.depth_limit,
    };
    let delete_authority = DeleteAuthority {
        namespace: namespace.as_fd(),
        root: root.as_fd(),
        marker: marker.as_ref().map(AsFd::as_fd),
        lease: lease.as_ref().map(AsFd::as_fd),
        quarantine: &quarantine,
        sidecar: authority.as_fd(),
        sidecar_name: &authority_name,
        sidecar_identity: &request.authority_identity,
        record: &record,
    };
    if let Err(error) = finish_delete(&delete_authority, &mut budget, phase) {
        let reason = if matches!(error.raw_os_error(), Some(libc::EFBIG | libc::ETIMEDOUT)) {
            RecoveryReason::BudgetExceeded
        } else {
            RecoveryReason::DeleteFailed
        };
        return Ok(recovery_pending(
            &quarantine_name,
            &request.authority_name,
            root.as_fd(),
            reason,
        ));
    }
    Ok(CollectionResponse {
        protocol_version: COLLECTION_PROTOCOL_VERSION,
        result: CollectionOutcome::Collected {
            entries: budget.entries,
        },
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::scratch_fs::test_support::{lock_for_owner, unlock_for_recovery};
    use std::fs::{self, File, OpenOptions};
    use std::os::fd::AsRawFd;
    use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct Fixture {
        namespace_path: PathBuf,
        root_path: PathBuf,
        namespace: File,
        root: File,
        marker: File,
        lease: File,
    }

    impl Fixture {
        fn new() -> Self {
            let suffix = NEXT.fetch_add(1, Ordering::Relaxed);
            let namespace_path = std::env::temp_dir().join(format!(
                "darling-scratch-collection-{}-{suffix}",
                std::process::id()
            ));
            fs::create_dir(&namespace_path).expect("create namespace");
            fs::set_permissions(&namespace_path, fs::Permissions::from_mode(0o700))
                .expect("mode namespace");
            let root_path = namespace_path.join("owned-root");
            fs::create_dir(&root_path).expect("create root");
            fs::write(root_path.join(".darling-scratch-v1"), b"marker\n").expect("write marker");
            fs::set_permissions(
                root_path.join(".darling-scratch-v1"),
                fs::Permissions::from_mode(0o600),
            )
            .expect("mode marker");
            fs::write(root_path.join(".darling-scratch-lease"), b"").expect("write lease");
            fs::set_permissions(
                root_path.join(".darling-scratch-lease"),
                fs::Permissions::from_mode(0o600),
            )
            .expect("mode lease");
            let directory = |path: &std::path::Path| {
                OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_DIRECTORY | libc::O_CLOEXEC | libc::O_NOFOLLOW)
                    .open(path)
                    .expect("open directory")
            };
            let namespace = directory(&namespace_path);
            let root = directory(&root_path);
            let marker = OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
                .open(root_path.join(".darling-scratch-v1"))
                .expect("open marker");
            let lease = OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
                .open(root_path.join(".darling-scratch-lease"))
                .expect("open lease");
            lock_for_owner(lease.as_fd()).expect("lock lease");
            Self {
                namespace_path,
                root_path,
                namespace,
                root,
                marker,
                lease,
            }
        }

        fn request(&self, entry_limit: usize, depth_limit: usize) -> CollectionRequest {
            let file_identity = |file: &File| identity(&fstat(file.as_fd()).expect("fstat"));
            CollectionRequest {
                protocol_version: COLLECTION_PROTOCOL_VERSION,
                operation: CollectionOperation::Collect,
                namespace_fd: self.namespace.as_raw_fd(),
                root_fd: self.root.as_raw_fd(),
                marker_fd: self.marker.as_raw_fd(),
                lease_fd: self.lease.as_raw_fd(),
                root_name: "owned-root".to_owned(),
                quarantine_name: ".gc-0123456789abcdef0123456789abcdef".to_owned(),
                authority_name: ".gc-0123456789abcdef0123456789abcdef.authority".to_owned(),
                namespace_identity: file_identity(&self.namespace),
                root_identity: file_identity(&self.root),
                marker_identity: file_identity(&self.marker),
                lease_identity: file_identity(&self.lease),
                process_limit: 65_536,
                fd_limit: 4_096,
                entry_limit,
                depth_limit,
                time_limit_ms: 10_000,
                output_limit_bytes: 1_048_576,
            }
        }

        fn isolate(&self, phase: u8) -> (File, CollectionRequest) {
            let request = self.request(100, 10);
            fs::write(self.root_path.join("payload"), b"payload").expect("payload");
            let record = AuthorityRecord {
                version: COLLECTION_PROTOCOL_VERSION,
                public_name: request.root_name.clone(),
                quarantine_name: request.quarantine_name.clone(),
                root_identity: immutable(&request.root_identity),
                root_links: request.root_identity.links,
                marker_identity: request.marker_identity.clone(),
                lease_identity: request.lease_identity.clone(),
            };
            let authority_name = component(&request.authority_name).expect("authority name");
            let (authority, _) = create_authority(self.namespace.as_fd(), &authority_name, &record)
                .expect("create authority");
            let source = component(&request.root_name).expect("source");
            let quarantine = component(&request.quarantine_name).expect("quarantine");
            rename_noreplace(self.namespace.as_fd(), &source, &quarantine).expect("isolate");
            sync(self.namespace.as_fd()).expect("namespace sync");
            set_phase(authority.as_fd(), b'Q').expect("phase Q");
            if matches!(phase, b'M' | b'L' | b'R') {
                fs::remove_file(
                    self.root_path
                        .with_file_name(&request.quarantine_name)
                        .join("payload"),
                )
                .expect("remove payload");
                set_phase(authority.as_fd(), b'M').expect("phase M");
            }
            if matches!(phase, b'L' | b'R') {
                fs::remove_file(
                    self.root_path
                        .with_file_name(&request.quarantine_name)
                        .join(".darling-scratch-v1"),
                )
                .expect("remove marker");
                set_phase(authority.as_fd(), b'L').expect("phase L");
            }
            if phase == b'R' {
                set_phase(authority.as_fd(), b'R').expect("phase R");
                fs::remove_file(
                    self.root_path
                        .with_file_name(&request.quarantine_name)
                        .join(".darling-scratch-lease"),
                )
                .expect("remove lease");
            }
            let authority = File::from(authority);
            (authority, request)
        }

        fn release_inherited(&mut self) {
            unlock_for_recovery(self.lease.as_fd()).expect("unlock lease");
            let null = || File::open("/dev/null").expect("open null");
            drop(std::mem::replace(&mut self.root, null()));
            drop(std::mem::replace(&mut self.marker, null()));
            drop(std::mem::replace(&mut self.lease, null()));
        }

        fn recover(&self, authority: &File, request: &CollectionRequest) -> CollectionResponse {
            recover(&RecoveryRequest {
                protocol_version: COLLECTION_PROTOCOL_VERSION,
                operation: RecoveryOperation::Recover,
                namespace_fd: self.namespace.as_raw_fd(),
                authority_fd: authority.as_raw_fd(),
                authority_name: request.authority_name.clone(),
                namespace_identity: identity(&fstat(self.namespace.as_fd()).expect("namespace")),
                authority_identity: identity(&fstat(authority.as_fd()).expect("authority")),
                process_limit: request.process_limit,
                fd_limit: request.fd_limit,
                entry_limit: request.entry_limit,
                depth_limit: request.depth_limit,
                time_limit_ms: request.time_limit_ms,
                output_limit_bytes: request.output_limit_bytes,
            })
            .expect("recovery")
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = fs::remove_dir_all(&self.namespace_path);
        }
    }

    #[test]
    fn collects_normal_root() {
        let fixture = Fixture::new();
        fs::write(fixture.root_path.join("payload"), b"payload").expect("payload");
        let response = collect(&fixture.request(100, 10)).expect("collection");
        assert!(matches!(
            response.result,
            CollectionOutcome::Collected { .. }
        ));
        assert!(!fixture.root_path.exists());
    }

    #[test]
    fn quarantine_collision_preserves_both_objects() {
        let fixture = Fixture::new();
        let collision = fixture
            .namespace_path
            .join(".gc-0123456789abcdef0123456789abcdef");
        fs::create_dir(&collision).expect("collision");
        fs::write(collision.join("sentinel"), b"replacement").expect("sentinel");
        let response = collect(&fixture.request(100, 10)).expect("collection");
        assert_eq!(
            response.result,
            CollectionOutcome::Retained {
                reason: RetainReason::IdentityMismatch
            }
        );
        assert_eq!(
            fs::read(collision.join("sentinel")).expect("read"),
            b"replacement"
        );
        assert!(fixture.root_path.exists());
    }

    #[test]
    fn root_replacement_before_isolation_is_retained() {
        let fixture = Fixture::new();
        let request = fixture.request(100, 10);
        let parked = fixture.namespace_path.join("parked-original");
        fs::rename(&fixture.root_path, &parked).expect("park original");
        fs::create_dir(&fixture.root_path).expect("replacement root");
        fs::write(fixture.root_path.join("sentinel"), b"replacement").expect("replacement");
        let response = collect(&request).expect("collection");
        assert_eq!(
            response.result,
            CollectionOutcome::Retained {
                reason: RetainReason::IdentityMismatch
            }
        );
        assert_eq!(
            fs::read(fixture.root_path.join("sentinel")).expect("read"),
            b"replacement"
        );
        assert!(parked.exists());
    }

    #[test]
    fn marker_replacement_before_isolation_is_retained() {
        let fixture = Fixture::new();
        let request = fixture.request(100, 10);
        let marker_path = fixture.root_path.join(".darling-scratch-v1");
        fs::rename(&marker_path, fixture.root_path.join("parked-marker")).expect("park marker");
        fs::write(&marker_path, b"replacement").expect("replacement marker");
        fs::set_permissions(&marker_path, fs::Permissions::from_mode(0o600))
            .expect("mode replacement");
        let response = collect(&request).expect("collection");
        assert_eq!(
            response.result,
            CollectionOutcome::Retained {
                reason: RetainReason::IdentityMismatch
            }
        );
        assert_eq!(fs::read(marker_path).expect("read"), b"replacement");
        assert!(fixture.root_path.exists());
    }

    #[test]
    fn mount_census_decodes_and_detects_nested_mountpoint() {
        let deadline = Instant::now() + Duration::from_secs(1);
        let payload = b"25 31 0:23 / /tmp/owned\\040root/mounted rw - tmpfs tmpfs rw\n";
        assert!(
            mountinfo_contains(payload, std::path::Path::new("/tmp/owned root"), deadline,)
                .expect("mount census")
        );
    }

    #[test]
    fn bounded_failure_recovers_quarantine_without_touching_replacement() {
        let mut fixture = Fixture::new();
        let nested = fixture.root_path.join("one");
        fs::create_dir(&nested).expect("nested");
        fs::create_dir(nested.join("two")).expect("deep");
        fs::write(nested.join("two/payload"), b"payload").expect("payload");
        let request = fixture.request(100, 1);
        let response = collect(&request).expect("bounded collection");
        assert!(matches!(
            response.result,
            CollectionOutcome::Quarantined { .. }
        ));
        assert!(!fixture.root_path.exists());
        fs::create_dir(&fixture.root_path).expect("replacement root");
        fs::write(fixture.root_path.join("sentinel"), b"replacement").expect("replacement");
        unlock_for_recovery(fixture.lease.as_fd()).expect("unlock lease");
        let null = || File::open("/dev/null").expect("open null");
        drop(std::mem::replace(&mut fixture.root, null()));
        drop(std::mem::replace(&mut fixture.marker, null()));
        drop(std::mem::replace(&mut fixture.lease, null()));
        let authority_path = fixture
            .namespace_path
            .join(".gc-0123456789abcdef0123456789abcdef.authority");
        let authority = OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
            .open(authority_path)
            .expect("open authority");
        let recovered = recover(&RecoveryRequest {
            protocol_version: COLLECTION_PROTOCOL_VERSION,
            operation: RecoveryOperation::Recover,
            namespace_fd: fixture.namespace.as_raw_fd(),
            authority_fd: authority.as_raw_fd(),
            authority_name: ".gc-0123456789abcdef0123456789abcdef.authority".to_owned(),
            namespace_identity: identity(&fstat(fixture.namespace.as_fd()).expect("namespace")),
            authority_identity: identity(&fstat(authority.as_fd()).expect("authority")),
            process_limit: request.process_limit,
            fd_limit: request.fd_limit,
            entry_limit: request.entry_limit,
            depth_limit: 10,
            time_limit_ms: request.time_limit_ms,
            output_limit_bytes: request.output_limit_bytes,
        })
        .expect("recovery");
        assert!(
            matches!(recovered.result, CollectionOutcome::Collected { .. }),
            "{recovered:?}"
        );
        assert_eq!(
            fs::read(fixture.root_path.join("sentinel")).expect("read"),
            b"replacement"
        );
        assert!(!fixture
            .namespace_path
            .join(".gc-0123456789abcdef0123456789abcdef")
            .exists());
    }

    #[test]
    fn every_terminal_phase_recovers_with_mutable_nlink() {
        for phase in [b'Q', b'M', b'L', b'R'] {
            let mut fixture = Fixture::new();
            let (authority, request) = fixture.isolate(phase);
            fixture.release_inherited();
            let response = fixture.recover(&authority, &request);
            assert!(
                matches!(response.result, CollectionOutcome::Collected { .. }),
                "phase={} response={response:?}",
                char::from(phase),
            );
            assert!(!fixture
                .namespace_path
                .join(&request.quarantine_name)
                .exists());
            assert!(!fixture
                .namespace_path
                .join(&request.authority_name)
                .exists());
        }
    }

    #[test]
    fn recovery_preserves_quarantine_and_anchor_replacements() {
        for replacement in ["quarantine", "marker", "lease", "authority"] {
            let mut fixture = Fixture::new();
            let (authority, request) = fixture.isolate(b'Q');
            let quarantine = fixture.namespace_path.join(&request.quarantine_name);
            match replacement {
                "quarantine" => {
                    fs::rename(
                        &quarantine,
                        fixture.namespace_path.join("parked-quarantine"),
                    )
                    .expect("park quarantine");
                    fs::create_dir(&quarantine).expect("replacement quarantine");
                    fs::write(quarantine.join("sentinel"), b"replacement").expect("sentinel");
                }
                "marker" | "lease" => {
                    let name = if replacement == "marker" {
                        MARKER
                    } else {
                        LEASE
                    };
                    let path = quarantine.join(OsString::from_vec(name.to_vec()));
                    fs::rename(&path, quarantine.join(format!("parked-{replacement}")))
                        .expect("park anchor");
                    fs::write(&path, b"replacement").expect("replacement anchor");
                    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                        .expect("replacement mode");
                }
                "authority" => {
                    let path = fixture.namespace_path.join(&request.authority_name);
                    fs::rename(&path, fixture.namespace_path.join("parked-authority"))
                        .expect("park authority");
                    fs::write(&path, b"replacement").expect("replacement authority");
                    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))
                        .expect("replacement mode");
                }
                _ => unreachable!(),
            }
            fixture.release_inherited();
            let response = fixture.recover(&authority, &request);
            assert!(matches!(
                response.result,
                CollectionOutcome::Retained { .. }
            ));
            let replacement_path = match replacement {
                "quarantine" => quarantine.join("sentinel"),
                "marker" => quarantine.join(".darling-scratch-v1"),
                "lease" => quarantine.join(".darling-scratch-lease"),
                "authority" => fixture.namespace_path.join(&request.authority_name),
                _ => unreachable!(),
            };
            assert!(replacement_path.exists(), "replacement={replacement}");
        }
    }

    #[test]
    fn reserved_names_and_lease_authority_are_enforced() {
        let fixture = Fixture::new();
        let mut request = fixture.request(100, 10);
        request.root_name = ".gc-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa".to_owned();
        assert!(collect(&request).is_err());
        drop(fixture);

        let fixture = Fixture::new();
        let request = fixture.request(100, 10);
        unlock_for_recovery(fixture.lease.as_fd()).expect("unlock lease");
        assert!(matches!(
            collect(&request).expect("Rust-acquired lease").result,
            CollectionOutcome::Collected { .. }
        ));

        let fixture = Fixture::new();
        let request = fixture.request(100, 10);
        unlock_for_recovery(fixture.lease.as_fd()).expect("unlock passed lease");
        let contender = OpenOptions::new()
            .read(true)
            .write(true)
            .open(fixture.root_path.join(".darling-scratch-lease"))
            .expect("open contender");
        lock_for_owner(contender.as_fd()).expect("lock contender");
        assert_eq!(
            collect(&request).expect("busy verdict").result,
            CollectionOutcome::Retained {
                reason: RetainReason::IdentityMismatch
            }
        );
    }

    #[test]
    fn authority_before_and_after_rename_recovers_in_fresh_owner() {
        for renamed in [false, true] {
            let mut fixture = Fixture::new();
            let request = fixture.request(100, 10);
            let record = AuthorityRecord {
                version: COLLECTION_PROTOCOL_VERSION,
                public_name: request.root_name.clone(),
                quarantine_name: request.quarantine_name.clone(),
                root_identity: immutable(&request.root_identity),
                root_links: request.root_identity.links,
                marker_identity: request.marker_identity.clone(),
                lease_identity: request.lease_identity.clone(),
            };
            let authority_name = component(&request.authority_name).expect("authority name");
            let (authority, _) =
                create_authority(fixture.namespace.as_fd(), &authority_name, &record)
                    .expect("authority");
            if renamed {
                rename_noreplace(
                    fixture.namespace.as_fd(),
                    &component(&request.root_name).expect("public"),
                    &component(&request.quarantine_name).expect("quarantine"),
                )
                .expect("rename");
                sync(fixture.namespace.as_fd()).expect("namespace sync");
            }
            let authority = File::from(authority);
            fixture.release_inherited();
            let response = fixture.recover(&authority, &request);
            assert!(matches!(
                response.result,
                CollectionOutcome::Collected { .. }
            ));
            assert!(!fixture
                .namespace_path
                .join(&request.authority_name)
                .exists());
            assert_eq!(fixture.root_path.exists(), !renamed);
        }
    }

    #[test]
    fn forged_ignore_and_invalid_recovery_names_are_rejected() {
        let fixture = Fixture::new();
        let request = fixture.request(100, 10);
        let mut value = serde_json::to_value(&request).expect("serialize request");
        value
            .as_object_mut()
            .expect("request object")
            .insert("ignored_descriptors".to_owned(), serde_json::json!([]));
        assert!(serde_json::from_value::<CollectionRequest>(value).is_err());

        let invalid = RecoveryRequest {
            protocol_version: COLLECTION_PROTOCOL_VERSION,
            operation: RecoveryOperation::Recover,
            namespace_fd: fixture.namespace.as_raw_fd(),
            authority_fd: fixture.marker.as_raw_fd(),
            authority_name: ".gc-not-a-uuid.authority".to_owned(),
            namespace_identity: identity(&fstat(fixture.namespace.as_fd()).expect("namespace")),
            authority_identity: identity(&fstat(fixture.marker.as_fd()).expect("marker")),
            process_limit: 1,
            fd_limit: 1,
            entry_limit: 1,
            depth_limit: 1,
            time_limit_ms: 1,
            output_limit_bytes: 256,
        };
        assert!(recover(&invalid).is_err());
    }

    #[test]
    fn mountinfo_limit_plus_one_is_rejected() {
        let payload = vec![b'x'; MAX_MOUNTINFO_BYTES + 1];
        let error = read_mountinfo_bounded(std::io::Cursor::new(payload)).expect_err("oversized");
        assert_eq!(error.raw_os_error(), Some(libc::EFBIG));
    }
}
