//! Additive, fd-relative materialization of the persistent guest home layout.
//!
//! Existing policy-valid objects are accepted without an ownership claim.  The
//! transaction only creates missing objects; it never removes or replaces a
//! public object.  Consequently a crash can leave only a policy-valid prefix
//! of the requested layout, which a fresh invocation can safely complete.

#[cfg(test)]
use std::sync::atomic::{AtomicU64, Ordering};
use std::{
    ffi::{CStr, CString},
    io,
    mem::MaybeUninit,
    os::fd::{AsRawFd, BorrowedFd, FromRawFd, OwnedFd, RawFd},
};

const USERS: &CStr = c"Users";
const SHARED: &CStr = c"Shared";
const LINK_NAMES: [&CStr; 8] = [
    c"LinuxHome",
    c"Desktop",
    c"Downloads",
    c"Public",
    c"Documents",
    c"Music",
    c"Pictures",
    c"Movies",
];
const MAX_LOGIN: usize = 255;
const MAX_TARGET: usize = 4095;
const SHARED_MODE: u32 = 0o777;
const USER_MODE: u32 = 0o755;
#[cfg(test)]
static TRANSACTION: AtomicU64 = AtomicU64::new(1);

#[cfg(test)]
type TestHook = Box<dyn FnMut(u8) -> bool>;

#[cfg(test)]
thread_local! {
    static FAULT: std::cell::Cell<u8> = const { std::cell::Cell::new(0) };
    static HOOK: std::cell::RefCell<Option<TestHook>> = const { std::cell::RefCell::new(None) };
}

#[cfg(test)]
fn checkpoint(point: u8) -> Result<(), UserHomeError> {
    HOOK.with(|slot| {
        let mut slot = slot.borrow_mut();
        if let Some(hook) = slot.as_mut() {
            if hook(point) {
                *slot = None;
            }
        }
    });
    FAULT.with(|fault| {
        if fault.get() == point {
            fault.set(0);
            Err(UserHomeError::protocol("injected user-home interruption"))
        } else {
            Ok(())
        }
    })
}

#[cfg(not(test))]
fn checkpoint(_: u8) -> Result<(), UserHomeError> {
    Ok(())
}

#[derive(Debug)]
pub struct UserHomeError {
    operation: &'static str,
    source: Option<io::Error>,
}

impl UserHomeError {
    fn protocol(operation: &'static str) -> Self {
        Self {
            operation,
            source: None,
        }
    }

    fn io(operation: &'static str) -> Self {
        Self {
            operation,
            source: Some(io::Error::last_os_error()),
        }
    }
}

impl std::fmt::Display for UserHomeError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if let Some(source) = &self.source {
            write!(formatter, "{}: {source}", self.operation)
        } else {
            formatter.write_str(self.operation)
        }
    }
}

impl std::error::Error for UserHomeError {}

#[derive(Clone, Debug)]
pub struct UserHomePlan {
    pub login: Vec<u8>,
    pub targets: [Option<Vec<u8>>; 8],
    pub owner_uid: u32,
    pub owner_gid: u32,
    pub shared_mode: u32,
    pub user_mode: u32,
}

#[repr(C)]
pub struct DarlingLifecycleUserHomePlan {
    schema_version: u32,
    owner_uid: u32,
    owner_gid: u32,
    shared_mode: u32,
    user_mode: u32,
    reserved: u32,
    login: *const libc::c_char,
    targets: [*const libc::c_char; 8],
}

unsafe fn bounded_c_bytes(
    value: *const libc::c_char,
    maximum: usize,
    required: bool,
) -> Result<Option<Vec<u8>>, UserHomeError> {
    if value.is_null() {
        return if required {
            Err(UserHomeError::protocol("missing guest home plan value"))
        } else {
            Ok(None)
        };
    }
    let length = unsafe { libc::strnlen(value, maximum + 1) };
    if length == 0 || length > maximum {
        return Err(UserHomeError::protocol("invalid guest home plan value"));
    }
    Ok(Some(unsafe {
        std::slice::from_raw_parts(value.cast::<u8>(), length).to_vec()
    }))
}

/// Parse the bounded C user-home plan without borrowing caller-owned strings.
///
/// # Safety
/// Every non-null pointer must remain readable and NUL-terminated within its
/// field bound for the duration of this call.
pub unsafe fn parse_ffi_plan(
    plan: &DarlingLifecycleUserHomePlan,
) -> Result<UserHomePlan, UserHomeError> {
    if plan.schema_version != 1 || plan.reserved != 0 {
        return Err(UserHomeError::protocol("invalid guest home plan schema"));
    }
    let login = unsafe { bounded_c_bytes(plan.login, MAX_LOGIN, true) }?
        .ok_or_else(|| UserHomeError::protocol("missing guest login"))?;
    let mut targets = std::array::from_fn(|_| None);
    for (index, value) in plan.targets.iter().enumerate() {
        targets[index] = unsafe { bounded_c_bytes(*value, MAX_TARGET, index == 0) }?;
    }
    Ok(UserHomePlan {
        login,
        targets,
        owner_uid: plan.owner_uid,
        owner_gid: plan.owner_gid,
        shared_mode: plan.shared_mode,
        user_mode: plan.user_mode,
    })
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct Identity {
    device: u64,
    inode: u64,
    file_type: u32,
    mode: u32,
    uid: u32,
    gid: u32,
}

fn identity(fd: RawFd) -> Result<Identity, UserHomeError> {
    let mut status = MaybeUninit::<libc::stat>::uninit();
    if unsafe { libc::fstat(fd, status.as_mut_ptr()) } != 0 {
        return Err(UserHomeError::io("inspect retained object"));
    }
    Ok(identity_from(unsafe { status.assume_init() }))
}

fn identity_from(status: libc::stat) -> Identity {
    Identity {
        device: status.st_dev,
        inode: status.st_ino,
        file_type: status.st_mode & libc::S_IFMT,
        mode: status.st_mode & 0o7777,
        uid: status.st_uid,
        gid: status.st_gid,
    }
}

fn named_identity(parent: RawFd, name: &CStr) -> Result<Option<Identity>, UserHomeError> {
    let mut status = MaybeUninit::<libc::stat>::uninit();
    if unsafe {
        libc::fstatat(
            parent,
            name.as_ptr(),
            status.as_mut_ptr(),
            libc::AT_SYMLINK_NOFOLLOW,
        )
    } == 0
    {
        return Ok(Some(identity_from(unsafe { status.assume_init() })));
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ENOENT) {
        Ok(None)
    } else {
        Err(UserHomeError {
            operation: "inspect layout name",
            source: Some(error),
        })
    }
}

fn open_directory(parent: RawFd, name: &CStr) -> Result<OwnedFd, UserHomeError> {
    let fd = unsafe {
        libc::openat(
            parent,
            name.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        Err(UserHomeError::io("open layout directory"))
    } else {
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }
}

fn safe_directory(value: Identity, uid: u32, gid: u32, base_mode: u32) -> bool {
    value.file_type == libc::S_IFDIR
        && value.uid == uid
        && value.gid == gid
        && value.mode & !base_mode == 0
        && value.mode & 0o700 == 0o700
}

fn validate_opened_name(
    parent: RawFd,
    name: &CStr,
    opened: RawFd,
) -> Result<Identity, UserHomeError> {
    let retained = identity(opened)?;
    let named = named_identity(parent, name)?
        .ok_or_else(|| UserHomeError::protocol("layout name disappeared"))?;
    if retained.device != named.device || retained.inode != named.inode {
        return Err(UserHomeError::protocol("layout name replacement"));
    }
    Ok(retained)
}

fn ensure_directory(
    parent: RawFd,
    name: &CStr,
    uid: u32,
    gid: u32,
    mode: u32,
) -> Result<OwnedFd, UserHomeError> {
    if let Some(observed) = named_identity(parent, name)? {
        if !safe_directory(observed, uid, gid, mode) {
            return Err(UserHomeError::protocol("conflicting layout directory"));
        }
        checkpoint(1)?;
        let opened = open_directory(parent, name)?;
        let retained = validate_opened_name(parent, name, opened.as_raw_fd())?;
        if retained != observed {
            return Err(UserHomeError::protocol("layout directory changed"));
        }
        return Ok(opened);
    }

    checkpoint(2)?;
    if unsafe { libc::mkdirat(parent, name.as_ptr(), mode) } != 0 {
        return Err(UserHomeError::io("create layout directory"));
    }
    checkpoint(3)?;
    let opened = open_directory(parent, name)?;
    let published = validate_opened_name(parent, name, opened.as_raw_fd())?;
    if !safe_directory(published, uid, gid, mode) {
        return Err(UserHomeError::protocol("created layout directory mismatch"));
    }
    Ok(opened)
}

struct RetainedSymlink {
    fd: OwnedFd,
}

impl RetainedSymlink {
    fn open(parent: RawFd, name: &CStr) -> Result<Self, UserHomeError> {
        let fd = unsafe {
            libc::openat(
                parent,
                name.as_ptr(),
                libc::O_PATH | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if fd < 0 {
            Err(UserHomeError::io("retain layout symlink"))
        } else {
            Ok(Self {
                fd: unsafe { OwnedFd::from_raw_fd(fd) },
            })
        }
    }

    fn validate_name(&self, parent: RawFd, name: &CStr) -> Result<Identity, UserHomeError> {
        validate_opened_name(parent, name, self.fd.as_raw_fd())
    }

    fn target(&self) -> Result<Vec<u8>, UserHomeError> {
        let mut output = vec![0u8; MAX_TARGET + 1];
        let count = unsafe {
            libc::readlinkat(
                self.fd.as_raw_fd(),
                c"".as_ptr(),
                output.as_mut_ptr().cast(),
                output.len(),
            )
        };
        if count < 0 {
            return Err(UserHomeError::io("read layout symlink"));
        }
        let count = usize::try_from(count).map_err(|_| UserHomeError::protocol("link size"))?;
        if count > MAX_TARGET {
            return Err(UserHomeError::protocol("layout symlink target too long"));
        }
        output.truncate(count);
        Ok(output)
    }
}

fn exact_symlink(value: Identity, uid: u32, gid: u32) -> bool {
    value.file_type == libc::S_IFLNK && value.uid == uid && value.gid == gid
}

fn ensure_symlink(
    parent: RawFd,
    name: &CStr,
    target: &[u8],
    uid: u32,
    gid: u32,
) -> Result<(), UserHomeError> {
    let target_c = CString::new(target)
        .map_err(|_| UserHomeError::protocol("invalid layout symlink target"))?;
    if named_identity(parent, name)?.is_some() {
        checkpoint(7)?;
        let retained = RetainedSymlink::open(parent, name)?;
        checkpoint(8)?;
        let opened = retained.validate_name(parent, name)?;
        if !exact_symlink(opened, uid, gid) || retained.target()? != target {
            return Err(UserHomeError::protocol("conflicting layout symlink"));
        }
        checkpoint(4)?;
        retained.validate_name(parent, name)?;
        return Ok(());
    }

    checkpoint(5)?;
    if unsafe { libc::symlinkat(target_c.as_ptr(), parent, name.as_ptr()) } != 0 {
        return Err(UserHomeError::io("create layout symlink"));
    }
    checkpoint(6)?;
    let retained = RetainedSymlink::open(parent, name)?;
    let published = retained.validate_name(parent, name)?;
    if !exact_symlink(published, uid, gid) || retained.target()? != target {
        return Err(UserHomeError::protocol("published layout symlink changed"));
    }
    Ok(())
}

fn validate_plan(plan: &UserHomePlan) -> Result<(), UserHomeError> {
    if plan.login.is_empty()
        || plan.login.len() > MAX_LOGIN
        || plan.login == b"."
        || plan.login == b".."
        || plan.login.contains(&b'/')
        || plan.login.contains(&0)
    {
        return Err(UserHomeError::protocol("invalid login component"));
    }
    if plan.owner_uid != unsafe { libc::geteuid() } || plan.owner_gid != unsafe { libc::getegid() }
    {
        return Err(UserHomeError::protocol(
            "home owner differs from controller credentials",
        ));
    }
    if plan.shared_mode != SHARED_MODE || plan.user_mode != USER_MODE {
        return Err(UserHomeError::protocol("invalid home directory mode"));
    }
    for target in plan.targets.iter().flatten() {
        let relative = target.strip_prefix(b"/Volumes/SystemRoot/" as &[u8]);
        if target.is_empty()
            || target.len() > MAX_TARGET
            || target.contains(&0)
            || target.ends_with(b"/")
            || relative.is_none_or(|relative| {
                relative.split(|byte| *byte == b'/').any(|component| {
                    component.is_empty() || component == b"." || component == b".."
                })
            })
        {
            return Err(UserHomeError::protocol("invalid home symlink target"));
        }
    }
    if plan.targets[0].is_none() {
        return Err(UserHomeError::protocol("missing LinuxHome target"));
    }
    Ok(())
}

pub fn prepare(prefix: BorrowedFd<'_>, plan: &UserHomePlan) -> Result<(), UserHomeError> {
    validate_plan(plan)?;
    let prefix = prefix.as_raw_fd();
    let users = ensure_directory(
        prefix,
        USERS,
        plan.owner_uid,
        plan.owner_gid,
        plan.shared_mode,
    )?;
    let users_identity = identity(users.as_raw_fd())?;
    let shared = ensure_directory(
        users.as_raw_fd(),
        SHARED,
        plan.owner_uid,
        plan.owner_gid,
        plan.shared_mode,
    )?;
    let shared_identity = identity(shared.as_raw_fd())?;
    let login = CString::new(plan.login.as_slice())
        .map_err(|_| UserHomeError::protocol("invalid login component"))?;
    let home = ensure_directory(
        users.as_raw_fd(),
        &login,
        plan.owner_uid,
        plan.owner_gid,
        plan.user_mode,
    )?;
    let home_identity = identity(home.as_raw_fd())?;
    for (index, target) in plan.targets.iter().enumerate() {
        if let Some(target) = target {
            ensure_symlink(
                home.as_raw_fd(),
                LINK_NAMES[index],
                target,
                plan.owner_uid,
                plan.owner_gid,
            )?;
        }
    }
    if validate_opened_name(prefix, USERS, users.as_raw_fd())? != users_identity
        || validate_opened_name(users.as_raw_fd(), SHARED, shared.as_raw_fd())? != shared_identity
        || validate_opened_name(users.as_raw_fd(), &login, home.as_raw_fd())? != home_identity
    {
        return Err(UserHomeError::protocol("home parent replacement"));
    }
    for (index, target) in plan.targets.iter().enumerate() {
        if let Some(target) = target {
            let retained = RetainedSymlink::open(home.as_raw_fd(), LINK_NAMES[index])?;
            let observed = retained.validate_name(home.as_raw_fd(), LINK_NAMES[index])?;
            if !exact_symlink(observed, plan.owner_uid, plan.owner_gid)
                || retained.target()? != *target
            {
                return Err(UserHomeError::protocol("home symlink replacement"));
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::fd::AsFd, os::unix::fs::MetadataExt, path::PathBuf};

    fn root() -> (PathBuf, OwnedFd) {
        let path = std::env::temp_dir().join(format!(
            "darling-user-home-{}-{}",
            unsafe { libc::getpid() },
            TRANSACTION.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).unwrap();
        let c = CString::new(path.as_os_str().as_encoded_bytes()).unwrap();
        let fd = unsafe {
            OwnedFd::from_raw_fd(libc::open(
                c.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
            ))
        };
        (path, fd)
    }

    fn plan() -> UserHomePlan {
        UserHomePlan {
            login: b"tester".to_vec(),
            targets: [
                Some(b"/Volumes/SystemRoot/home/tester".to_vec()),
                Some(b"/Volumes/SystemRoot/home/tester/Desktop".to_vec()),
                None,
                None,
                None,
                None,
                None,
                None,
            ],
            owner_uid: unsafe { libc::geteuid() },
            owner_gid: unsafe { libc::getegid() },
            shared_mode: SHARED_MODE,
            user_mode: USER_MODE,
        }
    }

    fn inject(point: u8) {
        FAULT.with(|fault| fault.set(point));
    }

    fn hook(function: impl FnMut(u8) -> bool + 'static) {
        HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(function)));
    }

    fn prepare_with_umask(fd: &OwnedFd, mask: libc::mode_t) {
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::umask(mask) };
            let status = i32::from(prepare(fd.as_fd(), &plan()).is_err());
            unsafe { libc::_exit(status) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert!(libc::WIFEXITED(status));
        assert_eq!(libc::WEXITSTATUS(status), 0);
    }

    #[test]
    fn creates_and_reuses_exact_persistent_layout() {
        let (path, fd) = root();
        prepare(fd.as_fd(), &plan()).unwrap();
        let inode = fs::symlink_metadata(path.join("Users/tester/LinuxHome"))
            .unwrap()
            .ino();
        prepare(fd.as_fd(), &plan()).unwrap();
        assert_eq!(
            fs::symlink_metadata(path.join("Users/tester/LinuxHome"))
                .unwrap()
                .ino(),
            inode
        );
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn preserves_conflicts_and_partial_layout_completes_on_restart() {
        let (path, fd) = root();
        fs::create_dir(path.join("Users")).unwrap();
        fs::set_permissions(
            path.join("Users"),
            std::os::unix::fs::PermissionsExt::from_mode(SHARED_MODE),
        )
        .unwrap();
        fs::write(path.join("Users/Shared"), b"foreign").unwrap();
        let before = fs::symlink_metadata(path.join("Users/Shared")).unwrap();
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        let after = fs::symlink_metadata(path.join("Users/Shared")).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        assert_eq!(fs::read(path.join("Users/Shared")).unwrap(), b"foreign");
        fs::remove_file(path.join("Users/Shared")).unwrap();
        prepare(fd.as_fd(), &plan()).unwrap();
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn wrong_mode_owner_and_symlink_replacement_fail_closed() {
        let (path, fd) = root();
        prepare(fd.as_fd(), &plan()).unwrap();
        for invalid_mode in [0o777, 0o4755, 0o655] {
            fs::set_permissions(
                path.join("Users/tester"),
                std::os::unix::fs::PermissionsExt::from_mode(invalid_mode),
            )
            .unwrap();
            assert!(prepare(fd.as_fd(), &plan()).is_err());
        }
        fs::set_permissions(
            path.join("Users/tester"),
            std::os::unix::fs::PermissionsExt::from_mode(USER_MODE),
        )
        .unwrap();
        fs::remove_file(path.join("Users/tester/LinuxHome")).unwrap();
        fs::write(path.join("Users/tester/LinuxHome"), b"replacement").unwrap();
        let inode = fs::metadata(path.join("Users/tester/LinuxHome"))
            .unwrap()
            .ino();
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        assert_eq!(
            fs::metadata(path.join("Users/tester/LinuxHome"))
                .unwrap()
                .ino(),
            inode
        );
        assert_eq!(
            fs::read(path.join("Users/tester/LinuxHome")).unwrap(),
            b"replacement"
        );
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn interruption_before_and_after_publication_is_restart_safe() {
        for point in [2, 3, 5, 6] {
            let (path, fd) = root();
            inject(point);
            assert!(prepare(fd.as_fd(), &plan()).is_err());
            prepare(fd.as_fd(), &plan()).unwrap();
            prepare(fd.as_fd(), &plan()).unwrap();
            assert_eq!(
                fs::read_link(path.join("Users/tester/LinuxHome")).unwrap(),
                std::path::Path::new("/Volumes/SystemRoot/home/tester")
            );
            assert!(fs::read_dir(&path).unwrap().all(|entry| !entry
                .unwrap()
                .file_name()
                .to_string_lossy()
                .starts_with(".darling-home-")));
            fs::remove_dir_all(path).unwrap();
        }
    }

    #[test]
    fn replacement_between_validation_and_open_is_preserved() {
        let (path, fd) = root();
        prepare(fd.as_fd(), &plan()).unwrap();
        let link = path.join("Users/tester/LinuxHome");
        let replacement = link.clone();
        hook(move |point| {
            if point != 4 {
                return false;
            }
            fs::remove_file(&replacement).unwrap();
            fs::write(&replacement, b"replacement-after-validation").unwrap();
            true
        });
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        let metadata = fs::symlink_metadata(&link).unwrap();
        assert!(metadata.is_file());
        assert_eq!(fs::read(&link).unwrap(), b"replacement-after-validation");
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn symlink_swap_and_restore_is_rejected_without_mutation() {
        let (path, fd) = root();
        prepare(fd.as_fd(), &plan()).unwrap();
        let link = path.join("Users/tester/LinuxHome");
        let saved = path.join("Users/tester/LinuxHome.saved");
        let original = fs::symlink_metadata(&link).unwrap();
        let hook_link = link.clone();
        let hook_saved = saved.clone();
        hook(move |point| match point {
            7 => {
                fs::rename(&hook_link, &hook_saved).unwrap();
                std::os::unix::fs::symlink("/hostile-target", &hook_link).unwrap();
                false
            }
            8 => {
                fs::remove_file(&hook_link).unwrap();
                fs::rename(&hook_saved, &hook_link).unwrap();
                true
            }
            _ => false,
        });
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        let restored = fs::symlink_metadata(&link).unwrap();
        assert_eq!(
            (restored.dev(), restored.ino()),
            (original.dev(), original.ino())
        );
        assert_eq!(
            fs::read_link(&link).unwrap(),
            std::path::Path::new("/Volumes/SystemRoot/home/tester")
        );
        assert!(!saved.exists());
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn traversal_targets_are_rejected_before_mutation() {
        for target in [
            b"/Volumes/SystemRoot/../etc".as_slice(),
            b"/Volumes/SystemRoot/home/./tester".as_slice(),
            b"/Volumes/SystemRoot/home//tester".as_slice(),
            b"/Volumes/SystemRoot/home/tester/".as_slice(),
            b"/Volumes/SystemRootish/home/tester".as_slice(),
        ] {
            let (path, fd) = root();
            let mut hostile = plan();
            hostile.targets[0] = Some(target.to_vec());
            assert!(prepare(fd.as_fd(), &hostile).is_err());
            assert!(!path.join("Users").exists());
            fs::remove_dir(path).unwrap();
        }
    }

    #[test]
    fn foreign_owner_plan_is_rejected_before_mutation() {
        let (path, fd) = root();
        let mut foreign = plan();
        foreign.owner_uid = foreign.owner_uid.wrapping_add(1);
        assert!(prepare(fd.as_fd(), &foreign).is_err());
        assert!(!path.join("Users").exists());
        fs::remove_dir(path).unwrap();
    }

    #[test]
    fn destination_replacement_before_publication_is_preserved() {
        let (path, fd) = root();
        let replacement = path.join("Users");
        hook(move |point| {
            if point != 2 {
                return false;
            }
            fs::write(&replacement, b"directory-name-replacement").unwrap();
            true
        });
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        assert_eq!(
            fs::read(path.join("Users")).unwrap(),
            b"directory-name-replacement"
        );
        fs::remove_file(path.join("Users")).unwrap();
        prepare(fd.as_fd(), &plan()).unwrap();

        fs::remove_file(path.join("Users/tester/LinuxHome")).unwrap();
        let link_replacement = path.join("Users/tester/LinuxHome");
        hook(move |point| {
            if point != 5 {
                return false;
            }
            fs::write(&link_replacement, b"link-name-replacement").unwrap();
            true
        });
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        assert_eq!(
            fs::read(path.join("Users/tester/LinuxHome")).unwrap(),
            b"link-name-replacement"
        );
        fs::remove_dir_all(path).unwrap();
    }

    #[test]
    fn special_and_symlink_directory_conflicts_are_never_followed() {
        let (path, fd) = root();
        std::os::unix::fs::symlink("foreign-target", path.join("Users")).unwrap();
        let before = fs::symlink_metadata(path.join("Users")).unwrap();
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        let after = fs::symlink_metadata(path.join("Users")).unwrap();
        assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
        assert_eq!(
            fs::read_link(path.join("Users")).unwrap(),
            std::path::Path::new("foreign-target")
        );
        fs::remove_file(path.join("Users")).unwrap();

        let fifo = CString::new(path.join("Users").as_os_str().as_encoded_bytes()).unwrap();
        assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
        let fifo_before = fs::symlink_metadata(path.join("Users")).unwrap();
        assert!(prepare(fd.as_fd(), &plan()).is_err());
        let fifo_after = fs::symlink_metadata(path.join("Users")).unwrap();
        assert_eq!(
            (fifo_before.dev(), fifo_before.ino(), fifo_before.mode()),
            (fifo_after.dev(), fifo_after.ino(), fifo_after.mode())
        );
        fs::remove_file(path.join("Users")).unwrap();
        fs::remove_dir(path).unwrap();
    }

    #[test]
    fn crash_after_mkdir_and_symlink_is_completed_by_fresh_invocation() {
        for point in [3, 6] {
            let (path, fd) = root();
            let child = unsafe { libc::fork() };
            assert!(child >= 0);
            if child == 0 {
                inject(point);
                let status = i32::from(prepare(fd.as_fd(), &plan()).is_ok());
                unsafe { libc::_exit(status) };
            }
            let mut status = 0;
            assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
            assert!(libc::WIFEXITED(status));
            assert_eq!(libc::WEXITSTATUS(status), 0);
            prepare(fd.as_fd(), &plan()).unwrap();
            assert!(path.join("Users/tester/LinuxHome").is_symlink());
            fs::remove_dir_all(path).unwrap();
        }
    }

    #[test]
    fn safe_modes_survive_cross_umask_reuse_in_both_directions() {
        for (first, second, expected_shared, expected_user) in
            [(0o002, 0o077, 0o775, 0o755), (0o077, 0o002, 0o700, 0o700)]
        {
            let (path, fd) = root();
            prepare_with_umask(&fd, first);
            let users = fs::metadata(path.join("Users")).unwrap();
            let home = fs::metadata(path.join("Users/tester")).unwrap();
            assert_eq!(users.mode() & 0o7777, expected_shared);
            assert_eq!(home.mode() & 0o7777, expected_user);
            let identities = ((users.dev(), users.ino()), (home.dev(), home.ino()));

            prepare_with_umask(&fd, second);
            let users = fs::metadata(path.join("Users")).unwrap();
            let home = fs::metadata(path.join("Users/tester")).unwrap();
            assert_eq!(users.mode() & 0o7777, expected_shared);
            assert_eq!(home.mode() & 0o7777, expected_user);
            assert_eq!(
                ((users.dev(), users.ino()), (home.dev(), home.ino())),
                identities
            );
            fs::remove_dir_all(path).unwrap();
        }
    }

    #[test]
    fn off_created_safe_layout_is_accepted_without_mode_rewrite() {
        let (path, fd) = root();
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            unsafe { libc::umask(0o002) };
            assert_eq!(
                unsafe { libc::mkdirat(fd.as_raw_fd(), c"Users".as_ptr(), 0o777) },
                0
            );
            let users = open_directory(fd.as_raw_fd(), c"Users").unwrap();
            assert_eq!(
                unsafe { libc::mkdirat(users.as_raw_fd(), c"Shared".as_ptr(), 0o777) },
                0
            );
            assert_eq!(
                unsafe { libc::mkdirat(users.as_raw_fd(), c"tester".as_ptr(), 0o755) },
                0
            );
            let home = open_directory(users.as_raw_fd(), c"tester").unwrap();
            assert_eq!(
                unsafe {
                    libc::symlinkat(
                        c"/Volumes/SystemRoot/home/tester".as_ptr(),
                        home.as_raw_fd(),
                        c"LinuxHome".as_ptr(),
                    )
                },
                0
            );
            unsafe { libc::_exit(0) };
        }
        let mut status = 0;
        assert_eq!(unsafe { libc::waitpid(child, &mut status, 0) }, child);
        assert_eq!(libc::WEXITSTATUS(status), 0);
        let before = fs::metadata(path.join("Users/tester")).unwrap();
        prepare(fd.as_fd(), &plan()).unwrap();
        let after = fs::metadata(path.join("Users/tester")).unwrap();
        assert_eq!(
            (after.dev(), after.ino(), after.mode()),
            (before.dev(), before.ino(), before.mode())
        );
        fs::remove_dir_all(path).unwrap();
    }
}
