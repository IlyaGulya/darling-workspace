//! Authenticated, fd-relative binding for the deployed E-UNION lower root.
#![deny(unsafe_code)]

use crate::inherited_fd::duplicate_cloexec;
use crate::FileIdentity;
use libc;
use rustix::fs::{AtFlags, Mode, OFlags, ResolveFlags, SeekFrom};
use std::collections::BTreeMap;
use std::ffi::CString;
use std::io;
use std::os::fd::{AsFd, AsRawFd, OwnedFd, RawFd};

pub const NAME: &[u8] = b".darling-runtime-lower-binding-v1";
const HEADER: &str = "DARLING_RUNTIME_LOWER_BINDING_V3";
const MAX_BYTES: usize = 2048;
const LOWER_DESTINATION: &str = "libexec/darling";
const CONTROLLER_DESTINATION: &str = "bin/darlingserver";
const WORKER_DESTINATION: &str = "libexec/darling-lifecycle-controller-worker";
const PROVENANCE: &str = "product-deployment-transaction-v3";
const PREFIX_STATE_NAME: &[u8] = b".darling-prefix-state-v3";

#[derive(Debug)]
pub enum BindingError {
    Io(&'static str, io::Error),
    Malformed(&'static str),
    Identity(&'static str),
}

impl std::fmt::Display for BindingError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(operation, error) => write!(formatter, "{operation}: {error}"),
            Self::Malformed(message) => {
                write!(formatter, "malformed runtime lower binding: {message}")
            }
            Self::Identity(message) => {
                write!(formatter, "runtime lower binding identity: {message}")
            }
        }
    }
}

impl std::error::Error for BindingError {}

fn component(value: &[u8]) -> Result<CString, BindingError> {
    if value.is_empty()
        || value == b"."
        || value == b".."
        || value.contains(&b'/')
        || value.contains(&0)
    {
        return Err(BindingError::Malformed("relative component"));
    }
    CString::new(value).map_err(|_| BindingError::Malformed("relative component"))
}

fn from_stat(stat: rustix::fs::Stat) -> FileIdentity {
    FileIdentity {
        device: stat.st_dev,
        inode: stat.st_ino,
        mode: stat.st_mode,
        nlink: stat.st_nlink,
        uid: stat.st_uid,
        gid: stat.st_gid,
    }
}

fn named_identity(parent: impl AsFd, name: &[u8]) -> Result<FileIdentity, BindingError> {
    let name = component(name)?;
    rustix::fs::statat(parent, name.as_c_str(), AtFlags::SYMLINK_NOFOLLOW)
        .map(from_stat)
        .map_err(|error| BindingError::Io("fstatat runtime binding", error.into()))
}

fn identity(fd: impl AsFd) -> Result<FileIdentity, BindingError> {
    rustix::fs::fstat(fd)
        .map(from_stat)
        .map_err(|error| BindingError::Io("fstat runtime binding", error.into()))
}

fn read_bounded(fd: impl AsFd) -> Result<Vec<u8>, BindingError> {
    let fd = fd.as_fd();
    rustix::fs::seek(fd, SeekFrom::Start(0))
        .map_err(|error| BindingError::Io("seek runtime binding", error.into()))?;
    let mut output = Vec::with_capacity(512);
    let mut buffer = [0u8; 256];
    loop {
        let count = match rustix::io::read(fd, &mut buffer) {
            Ok(count) => count,
            Err(rustix::io::Errno::INTR) => continue,
            Err(error) => return Err(BindingError::Io("read runtime binding", error.into())),
        };
        if count == 0 {
            return Ok(output);
        }
        if output.len() + count > MAX_BYTES {
            return Err(BindingError::Malformed("size budget"));
        }
        output.extend_from_slice(&buffer[..count]);
    }
}

fn prefix_generation(content: &[u8]) -> Result<u64, BindingError> {
    let text = std::str::from_utf8(content)
        .map_err(|_| BindingError::Malformed("prefix state encoding"))?;
    let generation = text
        .lines()
        .find_map(|line| line.strip_prefix("generation="))
        .ok_or(BindingError::Malformed("prefix generation"))?
        .parse::<u64>()
        .map_err(|_| BindingError::Malformed("prefix generation"))?;
    if generation == 0 {
        return Err(BindingError::Malformed("prefix generation"));
    }
    Ok(generation)
}

fn open_prefix_state(
    prefix: impl AsFd,
    expected_generation: u64,
    prefix_identity: FileIdentity,
) -> Result<(OwnedFd, Vec<u8>, FileIdentity, Vec<u8>), BindingError> {
    for name in [PREFIX_STATE_NAME] {
        let name_c = component(name)?;
        let state = match rustix::fs::openat(
            prefix.as_fd(),
            name_c.as_c_str(),
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        ) {
            Ok(state) => state,
            Err(rustix::io::Errno::NOENT) => {
                continue;
            }
            Err(error) => {
                return Err(BindingError::Io(
                    "open prefix state for runtime binding",
                    error.into(),
                ));
            }
        };
        let state_identity = identity(&state)?;
        let content = read_bounded(&state)?;
        if state_identity.mode & libc::S_IFMT != libc::S_IFREG
            || state_identity.mode & 0o7777 != 0o600
            || state_identity.uid != prefix_identity.uid
            || state_identity.gid != prefix_identity.gid
            || named_identity(prefix.as_fd(), name)? != state_identity
            || prefix_generation(&content)? != expected_generation
        {
            return Err(BindingError::Identity("prefix state generation"));
        }
        return Ok((state, name.to_vec(), state_identity, content));
    }
    Err(BindingError::Identity("missing prefix state"))
}

fn open_relative(prefix: impl AsFd, value: &str, directory: bool) -> Result<OwnedFd, BindingError> {
    if value.starts_with('/')
        || value
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(BindingError::Malformed("destination"));
    }
    let mut flags = OFlags::PATH | OFlags::NOFOLLOW | OFlags::CLOEXEC;
    if directory {
        flags |= OFlags::DIRECTORY;
    }
    rustix::fs::openat2(
        prefix,
        value,
        flags,
        Mode::empty(),
        ResolveFlags::NO_MAGICLINKS | ResolveFlags::NO_SYMLINKS | ResolveFlags::BENEATH,
    )
    .map_err(|error| BindingError::Io("openat2 runtime binding destination", error.into()))
}

fn parse_number(fields: &BTreeMap<&str, &str>, key: &'static str) -> Result<u64, BindingError> {
    fields
        .get(key)
        .ok_or(BindingError::Malformed("missing field"))?
        .parse::<u64>()
        .map_err(|_| BindingError::Malformed("numeric field"))
}

fn expected_identity(
    fields: &BTreeMap<&str, &str>,
    prefix: &'static str,
    file_type: u32,
) -> Result<FileIdentity, BindingError> {
    let key = |suffix| format!("{prefix}_{suffix}");
    let number = |suffix| {
        fields
            .get(key(suffix).as_str())
            .ok_or(BindingError::Malformed("identity field"))?
            .parse::<u64>()
            .map_err(|_| BindingError::Malformed("identity number"))
    };
    let mode = u32::try_from(number("mode")?).map_err(|_| BindingError::Malformed("mode"))?;
    Ok(FileIdentity {
        device: number("device")?,
        inode: number("inode")?,
        mode: file_type | mode,
        nlink: 1,
        uid: u32::try_from(number("uid")?).map_err(|_| BindingError::Malformed("uid"))?,
        gid: u32::try_from(number("gid")?).map_err(|_| BindingError::Malformed("gid"))?,
    })
}

fn metadata_matches(observed: FileIdentity, expected: FileIdentity) -> bool {
    observed.device == expected.device
        && observed.inode == expected.inode
        && observed.mode == expected.mode
        && observed.uid == expected.uid
        && observed.gid == expected.gid
}

pub struct RuntimeLowerBinding {
    binding: OwnedFd,
    binding_identity: FileIdentity,
    binding_content: Vec<u8>,
    prefix_state: OwnedFd,
    prefix_state_name: Vec<u8>,
    prefix_state_identity: FileIdentity,
    prefix_state_content: Vec<u8>,
    lower: OwnedFd,
    lower_identity: FileIdentity,
    controller_identity: FileIdentity,
    worker: OwnedFd,
    worker_identity: FileIdentity,
    transaction_id: [u8; 16],
    prefix_generation: u64,
}

impl RuntimeLowerBinding {
    pub fn acquire(
        session_prefix: RawFd,
        session_prefix_identity: FileIdentity,
        deployment_prefix: RawFd,
        deployment_prefix_identity: FileIdentity,
        prefix_generation: u64,
    ) -> Result<Self, BindingError> {
        let session_prefix = duplicate_cloexec(session_prefix, 3)
            .map_err(|error| BindingError::Io("retain session prefix", error))?;
        let deployment_prefix = duplicate_cloexec(deployment_prefix, 3)
            .map_err(|error| BindingError::Io("retain deployment prefix", error))?;
        let binding_name = component(NAME)?;
        let binding = rustix::fs::openat(
            &session_prefix,
            binding_name.as_c_str(),
            OFlags::RDONLY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| BindingError::Io("open runtime lower binding", error.into()))?;
        let binding_identity = identity(&binding)?;
        if binding_identity.mode & libc::S_IFMT != libc::S_IFREG
            || binding_identity.mode & 0o7777 != 0o600
            || binding_identity.nlink != 1
            || binding_identity.uid != session_prefix_identity.uid
            || binding_identity.gid != session_prefix_identity.gid
            || named_identity(&session_prefix, NAME)? != binding_identity
        {
            return Err(BindingError::Identity("binding object"));
        }
        let binding_content = read_bounded(&binding)?;
        let (prefix_state, prefix_state_name, prefix_state_identity, prefix_state_content) =
            open_prefix_state(&session_prefix, prefix_generation, session_prefix_identity)?;
        let text = std::str::from_utf8(&binding_content)
            .map_err(|_| BindingError::Malformed("encoding"))?;
        let body = text
            .strip_suffix('\n')
            .ok_or(BindingError::Malformed("terminator"))?;
        let mut lines = body.split('\n');
        if lines.next() != Some(HEADER) {
            return Err(BindingError::Malformed("header"));
        }
        let mut fields = BTreeMap::new();
        for line in lines {
            let (key, value) = line
                .split_once('=')
                .ok_or(BindingError::Malformed("field"))?;
            if key.is_empty() || value.is_empty() || fields.insert(key, value).is_some() {
                return Err(BindingError::Malformed("duplicate field"));
            }
        }
        if fields.len() != 29
            || parse_number(&fields, "schema_version")? != 3
            || fields.get("destination").copied() != Some(LOWER_DESTINATION)
            || fields.get("lower_type").copied() != Some("directory")
            || fields.get("controller_destination").copied() != Some(CONTROLLER_DESTINATION)
            || fields.get("controller_type").copied() != Some("regular")
            || fields.get("worker_destination").copied() != Some(WORKER_DESTINATION)
            || fields.get("worker_type").copied() != Some("regular")
            || fields.get("provenance").copied() != Some(PROVENANCE)
            || parse_number(&fields, "prefix_generation")? != prefix_generation
            || parse_number(&fields, "session_prefix_device")? != session_prefix_identity.device
            || parse_number(&fields, "session_prefix_inode")? != session_prefix_identity.inode
            || parse_number(&fields, "prefix_device")? != deployment_prefix_identity.device
            || parse_number(&fields, "prefix_inode")? != deployment_prefix_identity.inode
        {
            return Err(BindingError::Identity("manifest fields"));
        }
        let transaction_hex = fields
            .get("transaction_id")
            .ok_or(BindingError::Malformed("transaction id"))?;
        if transaction_hex.len() != 32
            || !transaction_hex.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(BindingError::Malformed("transaction id"));
        }
        let mut transaction_id = [0u8; 16];
        for (index, output) in transaction_id.iter_mut().enumerate() {
            *output = u8::from_str_radix(&transaction_hex[index * 2..index * 2 + 2], 16)
                .map_err(|_| BindingError::Malformed("transaction id"))?;
        }
        if transaction_id == [0; 16] {
            return Err(BindingError::Malformed("zero transaction id"));
        }
        let lower = open_relative(&deployment_prefix, LOWER_DESTINATION, true)?;
        let lower_identity = identity(&lower)?;
        let expected_lower = expected_identity(&fields, "lower", libc::S_IFDIR)?;
        if !metadata_matches(lower_identity, expected_lower) {
            return Err(BindingError::Identity("lower root"));
        }
        let controller = open_relative(&deployment_prefix, CONTROLLER_DESTINATION, false)?;
        let controller_identity = identity(&controller)?;
        let expected_controller = expected_identity(&fields, "controller", libc::S_IFREG)?;
        if !metadata_matches(controller_identity, expected_controller) {
            return Err(BindingError::Identity("deployed controller"));
        }
        let executable = rustix::fs::open(
            "/proc/self/exe",
            OFlags::PATH | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .map_err(|error| BindingError::Io("open controller executable", error.into()))?;
        if !metadata_matches(identity(&executable)?, controller_identity) {
            return Err(BindingError::Identity("running controller"));
        }
        let worker = open_relative(&deployment_prefix, WORKER_DESTINATION, false)?;
        let worker_identity = identity(&worker)?;
        let expected_worker = expected_identity(&fields, "worker", libc::S_IFREG)?;
        if !metadata_matches(worker_identity, expected_worker)
            || worker_identity.mode & 0o111 == 0
            || worker_identity.mode & 0o022 != 0
        {
            return Err(BindingError::Identity("controller worker"));
        }
        Ok(Self {
            binding,
            binding_identity,
            binding_content,
            prefix_state,
            prefix_state_name,
            prefix_state_identity,
            prefix_state_content,
            lower,
            lower_identity,
            controller_identity,
            worker,
            worker_identity,
            transaction_id,
            prefix_generation,
        })
    }

    pub fn lower_fd(&self) -> RawFd {
        self.lower.as_raw_fd()
    }

    pub fn worker_fd(&self) -> RawFd {
        self.worker.as_raw_fd()
    }

    pub fn revalidate(
        &self,
        session_prefix: RawFd,
        deployment_prefix: RawFd,
    ) -> Result<(), BindingError> {
        let session_prefix = duplicate_cloexec(session_prefix, 3)
            .map_err(|error| BindingError::Io("retain session prefix", error))?;
        let deployment_prefix = duplicate_cloexec(deployment_prefix, 3)
            .map_err(|error| BindingError::Io("retain deployment prefix", error))?;
        if identity(&self.binding)? != self.binding_identity
            || named_identity(&session_prefix, NAME)? != self.binding_identity
            || read_bounded(&self.binding)? != self.binding_content
        {
            return Err(BindingError::Identity("binding replacement"));
        }
        if identity(&self.prefix_state)? != self.prefix_state_identity
            || named_identity(&session_prefix, &self.prefix_state_name)?
                != self.prefix_state_identity
            || read_bounded(&self.prefix_state)? != self.prefix_state_content
            || prefix_generation(&self.prefix_state_content)? != self.prefix_generation
        {
            return Err(BindingError::Identity("prefix state replacement"));
        }
        let lower = open_relative(&deployment_prefix, LOWER_DESTINATION, true)?;
        if !metadata_matches(identity(&lower)?, self.lower_identity) {
            return Err(BindingError::Identity("lower replacement"));
        }
        let controller = open_relative(&deployment_prefix, CONTROLLER_DESTINATION, false)?;
        if !metadata_matches(identity(&controller)?, self.controller_identity) {
            return Err(BindingError::Identity("controller replacement"));
        }
        let worker = open_relative(&deployment_prefix, WORKER_DESTINATION, false)?;
        if !metadata_matches(identity(&worker)?, self.worker_identity)
            || identity(&self.worker)? != self.worker_identity
        {
            return Err(BindingError::Identity("worker replacement"));
        }
        if self.transaction_id == [0; 16] {
            return Err(BindingError::Identity("transaction identity"));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::os::unix::fs::{MetadataExt, PermissionsExt};
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    static NEXT: AtomicU64 = AtomicU64::new(1);

    struct Fixture {
        root: PathBuf,
    }

    impl Fixture {
        fn new() -> Self {
            let root = std::env::temp_dir().join(format!(
                "darling-runtime-binding-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::create_dir(&root).unwrap();
            fs::create_dir_all(root.join("libexec/darling")).unwrap();
            fs::create_dir_all(root.join("bin")).unwrap();
            fs::create_dir_all(root.join("libexec")).unwrap();
            fs::set_permissions(
                root.join("libexec/darling"),
                fs::Permissions::from_mode(0o755),
            )
            .unwrap();
            fs::hard_link(
                std::env::current_exe().unwrap(),
                root.join("bin/darlingserver"),
            )
            .unwrap();
            fs::copy(
                std::env::current_exe().unwrap(),
                root.join(WORKER_DESTINATION),
            )
            .unwrap();
            fs::set_permissions(
                root.join(WORKER_DESTINATION),
                fs::Permissions::from_mode(0o755),
            )
            .unwrap();
            let prefix = fs::metadata(&root).unwrap();
            let state = format!(
                "DARLING_PREFIX_STATE_V3\n\
                 schema_version=3\n\
                 runtime_mode=rootless-eunion\n\
                 generation=7\n\
                 prefix_device={}\n\
                 prefix_inode={}\n\
                 sidecar_device={}\n\
                 sidecar_inode={}\n\
                 owner_uid={}\n\
                 owner_gid={}\n\
                 provenance=darling-runtime-prefix-lifecycle-v3\n",
                prefix.dev(),
                prefix.ino(),
                prefix.dev(),
                prefix.ino(),
                prefix.uid(),
                prefix.gid(),
            );
            let state_path = root.join(".darling-prefix-state-v3");
            fs::write(&state_path, state).unwrap();
            fs::set_permissions(&state_path, fs::Permissions::from_mode(0o600)).unwrap();
            let fixture = Self { root };
            fixture.write_binding(7);
            fixture
        }

        fn prefix(&self) -> OwnedFd {
            rustix::fs::open(
                &self.root,
                OFlags::PATH | OFlags::DIRECTORY | OFlags::NOFOLLOW | OFlags::CLOEXEC,
                Mode::empty(),
            )
            .unwrap()
        }

        fn content(&self, generation: u64) -> String {
            let prefix = fs::metadata(&self.root).unwrap();
            let lower = fs::metadata(self.root.join("libexec/darling")).unwrap();
            let controller = fs::metadata(self.root.join("bin/darlingserver")).unwrap();
            let worker = fs::metadata(self.root.join(WORKER_DESTINATION)).unwrap();
            format!(
                "{HEADER}\n\
                 schema_version=3\n\
                 transaction_id=00112233445566778899aabbccddeeff\n\
                 prefix_generation={generation}\n\
                 session_prefix_device={}\n\
                 session_prefix_inode={}\n\
                 destination={LOWER_DESTINATION}\n\
                 prefix_device={}\n\
                 prefix_inode={}\n\
                 lower_device={}\n\
                 lower_inode={}\n\
                 lower_type=directory\n\
                 lower_mode={}\n\
                 lower_uid={}\n\
                 lower_gid={}\n\
                 controller_destination={CONTROLLER_DESTINATION}\n\
                 controller_device={}\n\
                 controller_inode={}\n\
                 controller_type=regular\n\
                 controller_mode={}\n\
                 controller_uid={}\n\
                 controller_gid={}\n\
                 worker_destination={WORKER_DESTINATION}\n\
                 worker_device={}\n\
                 worker_inode={}\n\
                 worker_type=regular\n\
                 worker_mode={}\n\
                 worker_uid={}\n\
                 worker_gid={}\n\
                 provenance={PROVENANCE}\n",
                prefix.dev(),
                prefix.ino(),
                prefix.dev(),
                prefix.ino(),
                lower.dev(),
                lower.ino(),
                lower.mode() & 0o7777,
                lower.uid(),
                lower.gid(),
                controller.dev(),
                controller.ino(),
                controller.mode() & 0o7777,
                controller.uid(),
                controller.gid(),
                worker.dev(),
                worker.ino(),
                worker.mode() & 0o7777,
                worker.uid(),
                worker.gid(),
            )
        }

        fn write_binding(&self, generation: u64) {
            let path = self.root.join(std::str::from_utf8(NAME).unwrap());
            fs::write(&path, self.content(generation)).unwrap();
            fs::set_permissions(path, fs::Permissions::from_mode(0o600)).unwrap();
        }

        fn acquire(&self, generation: u64) -> Result<RuntimeLowerBinding, BindingError> {
            let prefix = self.prefix();
            RuntimeLowerBinding::acquire(
                prefix.as_raw_fd(),
                identity(&prefix).unwrap(),
                prefix.as_raw_fd(),
                identity(&prefix).unwrap(),
                generation,
            )
        }
    }

    impl Drop for Fixture {
        fn drop(&mut self) {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }

    #[test]
    fn user_owned_runtime_root_and_restart_replay_are_accepted() {
        let fixture = Fixture::new();
        let first = fixture.acquire(7).unwrap();
        first
            .revalidate(fixture.prefix().as_raw_fd(), fixture.prefix().as_raw_fd())
            .unwrap();
        drop(first);
        fixture.acquire(7).unwrap();
    }

    #[test]
    fn stale_generation_and_source_root_are_rejected() {
        let fixture = Fixture::new();
        assert!(matches!(fixture.acquire(8), Err(BindingError::Identity(_))));
        let path = fixture.root.join(std::str::from_utf8(NAME).unwrap());
        let forged = fixture.content(7).replace(
            "destination=libexec/darling",
            "destination=source/install-root",
        );
        fs::write(path, forged).unwrap();
        assert!(matches!(fixture.acquire(7), Err(BindingError::Identity(_))));
    }

    #[test]
    fn cross_prefix_and_forged_metadata_are_rejected() {
        let source = Fixture::new();
        let target = Fixture::new();
        fs::copy(
            source.root.join(std::str::from_utf8(NAME).unwrap()),
            target.root.join(std::str::from_utf8(NAME).unwrap()),
        )
        .unwrap();
        assert!(matches!(target.acquire(7), Err(BindingError::Identity(_))));
        for field in [
            "lower_uid",
            "lower_gid",
            "lower_mode",
            "lower_device",
            "lower_inode",
        ] {
            source.write_binding(7);
            let path = source.root.join(std::str::from_utf8(NAME).unwrap());
            let content = fs::read_to_string(&path).unwrap();
            let line = content
                .lines()
                .find(|line| line.starts_with(field))
                .unwrap();
            fs::write(
                &path,
                content.replace(line, &format!("{field}=18446744073709551615")),
            )
            .unwrap();
            assert!(source.acquire(7).is_err(), "forged {field} was accepted");
        }
    }

    #[test]
    fn malformed_truncated_oversized_and_missing_bindings_fail_closed() {
        let fixture = Fixture::new();
        let path = fixture.root.join(std::str::from_utf8(NAME).unwrap());
        for content in [
            b"DARLING_RUNTIME_LOWER_BINDING_V1\n".to_vec(),
            vec![b'x'; MAX_BYTES + 1],
        ] {
            fs::write(&path, content).unwrap();
            assert!(fixture.acquire(7).is_err());
        }
        fs::remove_file(path).unwrap();
        assert!(fixture.acquire(7).is_err());
    }

    #[test]
    fn manifest_and_lower_replacements_after_validation_are_detected() {
        let fixture = Fixture::new();
        let binding = fixture.acquire(7).unwrap();
        let path = fixture.root.join(std::str::from_utf8(NAME).unwrap());
        fs::rename(&path, fixture.root.join("binding.retained")).unwrap();
        fixture.write_binding(7);
        assert!(binding
            .revalidate(fixture.prefix().as_raw_fd(), fixture.prefix().as_raw_fd())
            .is_err());

        fs::remove_file(&path).unwrap();
        fs::rename(fixture.root.join("binding.retained"), &path).unwrap();
        let binding = fixture.acquire(7).unwrap();
        let lower = fixture.root.join("libexec/darling");
        fs::rename(&lower, fixture.root.join("libexec/retained")).unwrap();
        fs::create_dir(&lower).unwrap();
        assert!(binding
            .revalidate(fixture.prefix().as_raw_fd(), fixture.prefix().as_raw_fd())
            .is_err());
    }

    #[test]
    fn prefix_generation_manifest_replacement_after_acquisition_is_rejected() {
        let fixture = Fixture::new();
        let binding = fixture.acquire(7).unwrap();
        let state = fixture.root.join(".darling-prefix-state-v3");
        let retained = fixture.root.join("prefix-state.retained");
        fs::rename(&state, &retained).unwrap();
        let replacement = fs::read_to_string(&retained)
            .unwrap()
            .replace("generation=7", "generation=8");
        fs::write(&state, replacement).unwrap();
        fs::set_permissions(&state, fs::Permissions::from_mode(0o600)).unwrap();
        assert!(binding
            .revalidate(fixture.prefix().as_raw_fd(), fixture.prefix().as_raw_fd())
            .is_err());
    }

    #[test]
    fn lower_symlink_and_controller_replacement_are_rejected() {
        let fixture = Fixture::new();
        let lower = fixture.root.join("libexec/darling");
        let retained = fixture.root.join("libexec/retained");
        fs::rename(&lower, &retained).unwrap();
        std::os::unix::fs::symlink(&retained, &lower).unwrap();
        assert!(fixture.acquire(7).is_err());
        fs::remove_file(&lower).unwrap();
        fs::rename(&retained, &lower).unwrap();
        let controller = fixture.root.join("bin/darlingserver");
        fs::remove_file(&controller).unwrap();
        fs::write(controller, b"replacement").unwrap();
        assert!(fixture.acquire(7).is_err());
    }

    #[test]
    fn traversal_magiclink_and_hostile_binding_metadata_are_rejected() {
        let fixture = Fixture::new();
        let prefix = fixture.prefix();
        for destination in ["../libexec/darling", "libexec//darling", "/libexec/darling"] {
            assert!(matches!(
                open_relative(&prefix, destination, true),
                Err(BindingError::Malformed("destination"))
            ));
        }
        let proc_root = rustix::fs::open(
            "/proc",
            OFlags::PATH | OFlags::DIRECTORY | OFlags::CLOEXEC,
            Mode::empty(),
        )
        .unwrap();
        assert!(
            open_relative(&proc_root, "self/exe", false).is_err(),
            "procfs magiclink was accepted"
        );

        let lower = fixture.root.join("libexec/darling");
        let retained = fixture.root.join("libexec/retained");
        fs::rename(&lower, &retained).unwrap();
        std::os::unix::fs::symlink("/proc/self/fd/0", &lower).unwrap();
        assert!(fixture.acquire(7).is_err(), "proc magiclink was accepted");
        fs::remove_file(&lower).unwrap();
        fs::rename(&retained, &lower).unwrap();

        let binding_path = fixture.root.join(std::str::from_utf8(NAME).unwrap());
        fs::set_permissions(&binding_path, fs::Permissions::from_mode(0o644)).unwrap();
        assert!(fixture.acquire(7).is_err(), "hostile mode was accepted");
        fs::set_permissions(&binding_path, fs::Permissions::from_mode(0o600)).unwrap();
        fs::hard_link(&binding_path, fixture.root.join("binding-hardlink")).unwrap();
        assert!(
            fixture.acquire(7).is_err(),
            "hardlinked binding was accepted"
        );
    }

    #[test]
    fn retained_descriptors_are_cloexec_and_survive_descriptor_reuse() {
        let fixture = Fixture::new();
        let binding = fixture.acquire(7).unwrap();
        for descriptor in [
            binding.binding.as_fd(),
            binding.prefix_state.as_fd(),
            binding.lower.as_fd(),
            binding.worker.as_fd(),
        ] {
            let flags = rustix::io::fcntl_getfd(descriptor).unwrap();
            assert!(flags.contains(rustix::io::FdFlags::CLOEXEC));
        }

        let mut transient = fixture.prefix();
        let reused_number = transient.as_raw_fd();
        let replacement = fixture.prefix();
        rustix::io::dup3(&replacement, &mut transient, rustix::io::DupFlags::CLOEXEC).unwrap();
        assert_eq!(transient.as_raw_fd(), reused_number);
        binding
            .revalidate(transient.as_raw_fd(), transient.as_raw_fd())
            .unwrap();

        let foreign = Fixture::new();
        let foreign_prefix = foreign.prefix();
        rustix::io::dup3(
            &foreign_prefix,
            &mut transient,
            rustix::io::DupFlags::CLOEXEC,
        )
        .unwrap();
        assert_eq!(transient.as_raw_fd(), reused_number);
        assert!(binding
            .revalidate(transient.as_raw_fd(), transient.as_raw_fd())
            .is_err());
    }

    #[test]
    fn controller_replacement_after_acquisition_is_rejected() {
        let fixture = Fixture::new();
        let binding = fixture.acquire(7).unwrap();
        let controller = fixture.root.join(CONTROLLER_DESTINATION);
        let retained = fixture.root.join("bin/controller.retained");
        fs::rename(&controller, &retained).unwrap();
        fs::copy(&retained, &controller).unwrap();

        assert!(matches!(
            binding.revalidate(fixture.prefix().as_raw_fd(), fixture.prefix().as_raw_fd()),
            Err(BindingError::Identity("controller replacement"))
        ));
        assert_eq!(fs::read(&retained).unwrap(), fs::read(&controller).unwrap());
    }

    #[test]
    fn symlinked_ancestor_is_rejected_without_traversal() {
        let fixture = Fixture::new();
        let libexec = fixture.root.join("libexec");
        let retained = fixture.root.join("libexec.retained");
        fs::rename(&libexec, &retained).unwrap();
        std::os::unix::fs::symlink(&retained, &libexec).unwrap();

        assert!(fixture.acquire(7).is_err());
        assert!(retained.join("darling").is_dir());
        assert!(retained
            .join("darling-lifecycle-controller-worker")
            .is_file());
    }

    #[test]
    fn worker_replacement_after_acquisition_is_rejected() {
        let fixture = Fixture::new();
        let binding = fixture.acquire(7).unwrap();
        let worker = fixture.root.join(WORKER_DESTINATION);
        let retained = fixture.root.join("libexec/worker.retained");
        fs::rename(&worker, &retained).unwrap();
        fs::copy(&retained, &worker).unwrap();
        fs::set_permissions(&worker, fs::Permissions::from_mode(0o755)).unwrap();

        assert!(matches!(
            binding.revalidate(fixture.prefix().as_raw_fd(), fixture.prefix().as_raw_fd()),
            Err(BindingError::Identity("worker replacement"))
        ));
        assert_eq!(fs::read(&retained).unwrap(), fs::read(&worker).unwrap());
    }
}
