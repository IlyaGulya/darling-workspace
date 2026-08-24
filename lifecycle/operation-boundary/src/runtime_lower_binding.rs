//! Authenticated, fd-relative binding for the deployed E-UNION lower root.

use crate::FileIdentity;
use libc::{self, c_int};
use std::collections::BTreeMap;
use std::ffi::CString;
use std::io;
use std::mem::size_of;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd};

pub const NAME: &[u8] = b".darling-runtime-lower-binding-v1";
const HEADER: &str = "DARLING_RUNTIME_LOWER_BINDING_V2";
const MAX_BYTES: usize = 2048;
const LOWER_DESTINATION: &str = "libexec/darling";
const CONTROLLER_DESTINATION: &str = "bin/darlingserver";
const PROVENANCE: &str = "product-deployment-transaction-v2";
const PREFIX_STATE_NAMES: [&[u8]; 2] = [b".darling-prefix-state-v3", b".darling-prefix-state-v2"];

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

fn io(operation: &'static str) -> BindingError {
    BindingError::Io(operation, io::Error::last_os_error())
}

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

fn named_identity(parent: RawFd, name: &[u8]) -> Result<FileIdentity, BindingError> {
    let name = component(name)?;
    FileIdentity::from_at(parent, &name).map_err(|_| io("fstatat runtime binding"))
}

fn identity(fd: RawFd) -> Result<FileIdentity, BindingError> {
    FileIdentity::from_fd(fd).map_err(|_| io("fstat runtime binding"))
}

fn read_bounded(fd: RawFd) -> Result<Vec<u8>, BindingError> {
    if unsafe { libc::lseek(fd, 0, libc::SEEK_SET) } < 0 {
        return Err(io("seek runtime binding"));
    }
    let mut output = Vec::with_capacity(512);
    let mut buffer = [0u8; 256];
    loop {
        let count = unsafe { libc::read(fd, buffer.as_mut_ptr().cast(), buffer.len()) };
        if count < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::EINTR) {
                continue;
            }
            return Err(io("read runtime binding"));
        }
        if count == 0 {
            return Ok(output);
        }
        let count = count as usize;
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
    prefix: RawFd,
    expected_generation: u64,
    prefix_identity: FileIdentity,
) -> Result<(OwnedFd, Vec<u8>, FileIdentity, Vec<u8>), BindingError> {
    for name in PREFIX_STATE_NAMES {
        let name_c = component(name)?;
        let fd = unsafe {
            libc::openat(
                prefix,
                name_c.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            if io::Error::last_os_error().raw_os_error() == Some(libc::ENOENT) {
                continue;
            }
            return Err(io("open prefix state for runtime binding"));
        }
        let state = unsafe { OwnedFd::from_raw_fd(fd) };
        let state_identity = identity(state.as_raw_fd())?;
        let content = read_bounded(state.as_raw_fd())?;
        if state_identity.mode & libc::S_IFMT != libc::S_IFREG
            || state_identity.mode & 0o7777 != 0o600
            || state_identity.uid != prefix_identity.uid
            || state_identity.gid != prefix_identity.gid
            || named_identity(prefix, name)? != state_identity
            || prefix_generation(&content)? != expected_generation
        {
            return Err(BindingError::Identity("prefix state generation"));
        }
        return Ok((state, name.to_vec(), state_identity, content));
    }
    Err(BindingError::Identity("missing prefix state"))
}

#[repr(C)]
struct OpenHow {
    flags: u64,
    mode: u64,
    resolve: u64,
}

fn open_relative(prefix: RawFd, value: &str, directory: bool) -> Result<OwnedFd, BindingError> {
    if value.starts_with('/')
        || value
            .split('/')
            .any(|part| part.is_empty() || part == "." || part == "..")
    {
        return Err(BindingError::Malformed("destination"));
    }
    let name = CString::new(value).map_err(|_| BindingError::Malformed("destination"))?;
    let how = OpenHow {
        flags: (libc::O_PATH
            | libc::O_NOFOLLOW
            | libc::O_CLOEXEC
            | if directory { libc::O_DIRECTORY } else { 0 }) as u64,
        mode: 0,
        // RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS | RESOLVE_BENEATH.
        resolve: 0x02 | 0x04 | 0x08,
    };
    let fd = unsafe {
        libc::syscall(
            libc::SYS_openat2,
            prefix,
            name.as_ptr(),
            &how,
            size_of::<OpenHow>(),
        ) as c_int
    };
    if fd < 0 {
        return Err(io("openat2 runtime binding destination"));
    }
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
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
        let binding_name = component(NAME)?;
        let binding_fd = unsafe {
            libc::openat(
                session_prefix,
                binding_name.as_ptr(),
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if binding_fd < 0 {
            return Err(io("open runtime lower binding"));
        }
        let binding = unsafe { OwnedFd::from_raw_fd(binding_fd) };
        let binding_identity = identity(binding.as_raw_fd())?;
        if binding_identity.mode & libc::S_IFMT != libc::S_IFREG
            || binding_identity.mode & 0o7777 != 0o600
            || binding_identity.nlink != 1
            || binding_identity.uid != session_prefix_identity.uid
            || binding_identity.gid != session_prefix_identity.gid
            || named_identity(session_prefix, NAME)? != binding_identity
        {
            return Err(BindingError::Identity("binding object"));
        }
        let binding_content = read_bounded(binding.as_raw_fd())?;
        let (prefix_state, prefix_state_name, prefix_state_identity, prefix_state_content) =
            open_prefix_state(session_prefix, prefix_generation, session_prefix_identity)?;
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
        if fields.len() != 22
            || parse_number(&fields, "schema_version")? != 2
            || fields.get("destination").copied() != Some(LOWER_DESTINATION)
            || fields.get("lower_type").copied() != Some("directory")
            || fields.get("controller_destination").copied() != Some(CONTROLLER_DESTINATION)
            || fields.get("controller_type").copied() != Some("regular")
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
        let lower = open_relative(deployment_prefix, LOWER_DESTINATION, true)?;
        let lower_identity = identity(lower.as_raw_fd())?;
        let expected_lower = expected_identity(&fields, "lower", libc::S_IFDIR)?;
        if !metadata_matches(lower_identity, expected_lower) {
            return Err(BindingError::Identity("lower root"));
        }
        let controller = open_relative(deployment_prefix, CONTROLLER_DESTINATION, false)?;
        let controller_identity = identity(controller.as_raw_fd())?;
        let expected_controller = expected_identity(&fields, "controller", libc::S_IFREG)?;
        if !metadata_matches(controller_identity, expected_controller) {
            return Err(BindingError::Identity("deployed controller"));
        }
        let executable_fd =
            unsafe { libc::open(c"/proc/self/exe".as_ptr(), libc::O_PATH | libc::O_CLOEXEC) };
        if executable_fd < 0 {
            return Err(io("open controller executable"));
        }
        let executable = unsafe { OwnedFd::from_raw_fd(executable_fd) };
        if !metadata_matches(identity(executable.as_raw_fd())?, controller_identity) {
            return Err(BindingError::Identity("running controller"));
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
            transaction_id,
            prefix_generation,
        })
    }

    pub fn lower_fd(&self) -> RawFd {
        self.lower.as_raw_fd()
    }

    pub fn revalidate(
        &self,
        session_prefix: RawFd,
        deployment_prefix: RawFd,
    ) -> Result<(), BindingError> {
        if identity(self.binding.as_raw_fd())? != self.binding_identity
            || named_identity(session_prefix, NAME)? != self.binding_identity
            || read_bounded(self.binding.as_raw_fd())? != self.binding_content
        {
            return Err(BindingError::Identity("binding replacement"));
        }
        if identity(self.prefix_state.as_raw_fd())? != self.prefix_state_identity
            || named_identity(session_prefix, &self.prefix_state_name)?
                != self.prefix_state_identity
            || read_bounded(self.prefix_state.as_raw_fd())? != self.prefix_state_content
            || prefix_generation(&self.prefix_state_content)? != self.prefix_generation
        {
            return Err(BindingError::Identity("prefix state replacement"));
        }
        let lower = open_relative(deployment_prefix, LOWER_DESTINATION, true)?;
        if !metadata_matches(identity(lower.as_raw_fd())?, self.lower_identity) {
            return Err(BindingError::Identity("lower replacement"));
        }
        let controller = open_relative(deployment_prefix, CONTROLLER_DESTINATION, false)?;
        if !metadata_matches(identity(controller.as_raw_fd())?, self.controller_identity) {
            return Err(BindingError::Identity("controller replacement"));
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
            let prefix = fs::metadata(&root).unwrap();
            let state = format!(
                "DARLING_PREFIX_STATE_V2\n\
                 schema_version=2\n\
                 runtime_mode=rootless-eunion\n\
                 generation=7\n\
                 prefix_device={}\n\
                 prefix_inode={}\n\
                 owner_uid={}\n\
                 owner_gid={}\n\
                 provenance=darling-runtime-prefix-lifecycle-v2\n",
                prefix.dev(),
                prefix.ino(),
                prefix.uid(),
                prefix.gid(),
            );
            let state_path = root.join(".darling-prefix-state-v2");
            fs::write(&state_path, state).unwrap();
            fs::set_permissions(&state_path, fs::Permissions::from_mode(0o600)).unwrap();
            let fixture = Self { root };
            fixture.write_binding(7);
            fixture
        }

        fn prefix(&self) -> OwnedFd {
            let path = CString::new(self.root.as_os_str().as_encoded_bytes()).unwrap();
            let fd = unsafe {
                libc::open(
                    path.as_ptr(),
                    libc::O_PATH | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
                )
            };
            assert!(fd >= 0);
            unsafe { OwnedFd::from_raw_fd(fd) }
        }

        fn content(&self, generation: u64) -> String {
            let prefix = fs::metadata(&self.root).unwrap();
            let lower = fs::metadata(self.root.join("libexec/darling")).unwrap();
            let controller = fs::metadata(self.root.join("bin/darlingserver")).unwrap();
            format!(
                "{HEADER}\n\
                 schema_version=2\n\
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
                identity(prefix.as_raw_fd()).unwrap(),
                prefix.as_raw_fd(),
                identity(prefix.as_raw_fd()).unwrap(),
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
        let state = fixture.root.join(".darling-prefix-state-v2");
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
}
