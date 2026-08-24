#![cfg(target_os = "linux")]

use darling_lifecycle_operation_boundary::cohort_routing::{
    darling_lifecycle_cohort_abandon, darling_lifecycle_cohort_start, CohortBootstrap,
};
use std::ffi::CString;
use std::fs;
use std::mem::MaybeUninit;
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::{MetadataExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

const ABANDON_PENDING: i32 = 3;

struct Fixture {
    root: PathBuf,
    sidecar: PathBuf,
}

impl Fixture {
    fn new(label: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "darling-cohort-child-forensic-{}-{label}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir(&root).unwrap();
        fs::create_dir_all(root.join("var/run")).unwrap();
        fs::create_dir_all(root.join("var/tmp/launchd")).unwrap();
        fs::set_permissions(root.join("var"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::set_permissions(root.join("var/run"), fs::Permissions::from_mode(0o755)).unwrap();
        fs::set_permissions(root.join("var/tmp"), fs::Permissions::from_mode(0o1777)).unwrap();
        fs::set_permissions(
            root.join("var/tmp/launchd"),
            fs::Permissions::from_mode(0o700),
        )
        .unwrap();
        let metadata = fs::metadata(&root).unwrap();
        let state = format!(
            "DARLING_PREFIX_STATE_V2\n\
             schema_version=2\n\
             runtime_mode=rootless-eunion\n\
             generation=1\n\
             prefix_device={}\n\
             prefix_inode={}\n\
             owner_uid={}\n\
             owner_gid={}\n\
             provenance=darling-runtime-prefix-lifecycle-v2\n",
            metadata.dev(),
            metadata.ino(),
            metadata.uid(),
            metadata.gid(),
        );
        fs::write(root.join(".darling-prefix-state-v2"), state).unwrap();
        fs::set_permissions(
            root.join(".darling-prefix-state-v2"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        let sidecar = root.parent().unwrap().join(format!(
            ".darling-lifecycle-{:x}-{:x}",
            metadata.dev(),
            metadata.ino()
        ));
        Self { root, sidecar }
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
        let entries: Vec<_> = fs::read_dir(&self.sidecar)
            .unwrap()
            .map(|entry| entry.unwrap().file_name())
            .collect();
        assert_eq!(entries, ["owner.lock"]);
        fs::remove_file(self.sidecar.join("owner.lock")).unwrap();
        fs::remove_dir(&self.sidecar).unwrap();
    }
}

fn write_exact(fd: i32, bytes: &[u8]) {
    let mut offset = 0;
    while offset < bytes.len() {
        let written =
            unsafe { libc::write(fd, bytes[offset..].as_ptr().cast(), bytes.len() - offset) };
        assert!(written > 0);
        offset += written as usize;
    }
}

fn read_exact(fd: i32, bytes: &mut [u8]) {
    let mut offset = 0;
    while offset < bytes.len() {
        let read = unsafe {
            libc::read(
                fd,
                bytes[offset..].as_mut_ptr().cast(),
                bytes.len() - offset,
            )
        };
        assert!(read > 0);
        offset += read as usize;
    }
}

fn only_child_pid() -> libc::pid_t {
    let children = fs::read_to_string(format!(
        "/proc/{}/task/{}/children",
        std::process::id(),
        std::process::id()
    ))
    .unwrap();
    let children: Vec<_> = children.split_whitespace().collect();
    assert_eq!(children.len(), 1, "unexpected controller child census");
    children[0].parse().unwrap()
}

fn start_controller(
    root: &Path,
) -> *mut darling_lifecycle_operation_boundary::cohort_routing::CohortController {
    let root_c = CString::new(root.as_os_str().as_bytes()).unwrap();
    let root_fd = unsafe {
        libc::open(
            root_c.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_CLOEXEC,
        )
    };
    assert!(root_fd >= 0);
    let root_fd = unsafe { OwnedFd::from_raw_fd(root_fd) };
    let mut bootstrap = MaybeUninit::<CohortBootstrap>::uninit();
    let controller = unsafe {
        darling_lifecycle_cohort_start(
            root_fd.as_raw_fd(),
            root_fd.as_raw_fd(),
            root_c.as_ptr(),
            std::process::id() as _,
            bootstrap.as_mut_ptr(),
        )
    };
    assert!(!controller.is_null());
    let bootstrap = unsafe { bootstrap.assume_init() };
    unsafe {
        libc::close(bootstrap.darlingserver_fd);
        libc::close(bootstrap.dserver_log_fd);
    }
    controller
}

fn wait_gone(pid: libc::pid_t) {
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        if unsafe { libc::kill(pid, 0) } < 0
            && std::io::Error::last_os_error().raw_os_error() == Some(libc::ESRCH)
        {
            return;
        }
        assert!(Instant::now() < deadline, "controller child remained live");
        std::thread::yield_now();
    }
}

fn process_state(pid: libc::pid_t) -> u8 {
    let stat = fs::read(format!("/proc/{pid}/stat")).unwrap();
    let close = stat.iter().rposition(|byte| *byte == b')').unwrap();
    *stat.get(close + 2).unwrap()
}

fn run_parent_death_case(label: &str, before_ack: bool) {
    let fixture = Fixture::new(label);
    let endpoint = fixture.root.join(".lc-v1.sock");
    let mut channel = [0; 2];
    assert_eq!(
        unsafe { libc::pipe2(channel.as_mut_ptr(), libc::O_CLOEXEC) },
        0
    );
    let parent = unsafe { libc::fork() };
    assert!(parent >= 0);
    if parent == 0 {
        unsafe { libc::close(channel[0]) };
        let controller = start_controller(&fixture.root);
        let child = only_child_pid();
        if before_ack {
            assert_eq!(unsafe { libc::kill(child, libc::SIGSTOP) }, 0);
            for _ in 0..3 {
                assert_eq!(
                    unsafe { darling_lifecycle_cohort_abandon(controller) },
                    ABANDON_PENDING
                );
            }
        } else {
            unsafe { &mut *controller }
                .acknowledge_forensic_preserve_for_contract()
                .unwrap();
            // The production child remains alive after ACK, with admission
            // closed, until it observes parent death or explicit reaping.
            assert_eq!(unsafe { libc::kill(child, 0) }, 0);
            assert_ne!(process_state(child), b'Z');
        }
        write_exact(channel[1], &(child as i32).to_ne_bytes());
        loop {
            unsafe { libc::pause() };
        }
    }
    unsafe { libc::close(channel[1]) };
    let mut child_bytes = [0u8; 4];
    read_exact(channel[0], &mut child_bytes);
    unsafe { libc::close(channel[0]) };
    let controller_child = i32::from_ne_bytes(child_bytes);
    let before = fs::symlink_metadata(&endpoint).unwrap();
    assert_eq!(unsafe { libc::kill(parent, libc::SIGKILL) }, 0);
    let mut status = 0;
    assert_eq!(unsafe { libc::waitpid(parent, &mut status, 0) }, parent);
    if before_ack {
        assert_eq!(unsafe { libc::kill(controller_child, libc::SIGCONT) }, 0);
    }
    wait_gone(controller_child);
    let after = fs::symlink_metadata(&endpoint).unwrap();
    assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
}

#[test]
fn production_controller_child_preserves_before_and_after_ack_parent_death() {
    run_parent_death_case("before-ack", true);
    run_parent_death_case("after-ack", false);
}
