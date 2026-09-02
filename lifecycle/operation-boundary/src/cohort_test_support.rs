use super::*;
use std::fs;
use std::os::unix::fs::{symlink, FileTypeExt, MetadataExt, PermissionsExt};
use std::path::PathBuf;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

static NEXT_TEST: AtomicU64 = AtomicU64::new(1);

struct Fixture {
    root: PathBuf,
}

impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "darling-lifecycle-cohort-{}-{}",
            std::process::id(),
            NEXT_TEST.fetch_add(1, Ordering::Relaxed)
        ));
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
        Self { root }
    }

    fn new_v3() -> Self {
        let fixture = Self::new();
        fs::remove_file(fixture.root.join(".darling-prefix-state-v2")).unwrap();
        let metadata = fs::metadata(&fixture.root).unwrap();
        let state = format!(
            "DARLING_PREFIX_STATE_V3\n\
                 schema_version=3\n\
                 runtime_mode=rootless-eunion\n\
                 generation=1\n\
                 prefix_device={}\n\
                 prefix_inode={}\n\
                 sidecar_device={}\n\
                 sidecar_inode={}\n\
                 owner_uid={}\n\
                 owner_gid={}\n\
                 provenance=darling-runtime-prefix-sidecar-v1\n",
            metadata.dev(),
            metadata.ino(),
            metadata.dev(),
            metadata.ino() + 1,
            metadata.uid(),
            metadata.gid(),
        );
        fs::write(fixture.root.join(".darling-prefix-state-v3"), state).unwrap();
        fs::set_permissions(
            fixture.root.join(".darling-prefix-state-v3"),
            fs::Permissions::from_mode(0o600),
        )
        .unwrap();
        fixture
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

fn publish_runtime_lower_binding(fixture: &Fixture) {
    let lower = fixture.root.join("libexec/darling");
    let controller = fixture.root.join("bin/darlingserver");
    fs::create_dir_all(&lower).unwrap();
    fs::create_dir_all(controller.parent().unwrap()).unwrap();
    fs::set_permissions(&lower, fs::Permissions::from_mode(0o755)).unwrap();
    fs::hard_link(std::env::current_exe().unwrap(), &controller).unwrap();
    let prefix = fs::metadata(&fixture.root).unwrap();
    let lower_metadata = fs::metadata(&lower).unwrap();
    let controller_metadata = fs::metadata(&controller).unwrap();
    let content = format!(
        "DARLING_RUNTIME_LOWER_BINDING_V2\n\
             schema_version=2\n\
             transaction_id=00112233445566778899aabbccddeeff\n\
             prefix_generation=1\n\
             session_prefix_device={}\n\
             session_prefix_inode={}\n\
             destination=libexec/darling\n\
             prefix_device={}\n\
             prefix_inode={}\n\
             lower_device={}\n\
             lower_inode={}\n\
             lower_type=directory\n\
             lower_mode={}\n\
             lower_uid={}\n\
             lower_gid={}\n\
             controller_destination=bin/darlingserver\n\
             controller_device={}\n\
             controller_inode={}\n\
             controller_type=regular\n\
             controller_mode={}\n\
             controller_uid={}\n\
             controller_gid={}\n\
             provenance=product-deployment-transaction-v2\n",
        prefix.dev(),
        prefix.ino(),
        prefix.dev(),
        prefix.ino(),
        lower_metadata.dev(),
        lower_metadata.ino(),
        lower_metadata.mode() & 0o7777,
        lower_metadata.uid(),
        lower_metadata.gid(),
        controller_metadata.dev(),
        controller_metadata.ino(),
        controller_metadata.mode() & 0o7777,
        controller_metadata.uid(),
        controller_metadata.gid(),
    );
    let path = fixture.root.join(".darling-runtime-lower-binding-v1");
    fs::write(&path, content).unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600)).unwrap();
}

#[test]
fn deployed_v3_prefix_state_is_retained_and_revalidated() {
    let fixture = Fixture::new_v3();
    let prefix = open_prefix(&fixture.root).unwrap();
    let prefix_identity = identity(prefix.as_raw_fd()).unwrap();
    let state = acquire_prefix_state(prefix.as_fd(), prefix_identity).unwrap();
    assert_eq!(state.name, PREFIX_STATE_V3_NAME);
    revalidate_prefix_state(prefix.as_fd(), prefix_identity, &state).unwrap();

    let path = fixture.root.join(".darling-prefix-state-v3");
    let mut content = fs::read(&path).unwrap();
    let generation = content
        .windows(b"generation=1".len())
        .position(|window| window == b"generation=1")
        .unwrap();
    content[generation + b"generation=".len()] = b'2';
    fs::write(path, content).unwrap();
    assert!(matches!(
        revalidate_prefix_state(prefix.as_fd(), prefix_identity, &state),
        Err(CohortError::Identity("runtime prefix state"))
    ));
}

#[test]
fn leased_acquisition_normalizes_legacy_endpoint_parent_mode() {
    let fixture = Fixture::new_v3();
    fs::set_permissions(
        fixture.root.join("var/tmp"),
        fs::Permissions::from_mode(0o755),
    )
    .unwrap();
    let authority =
        SessionAuthority::acquire(&fixture.root, std::process::id() as libc::pid_t).unwrap();
    assert_eq!(
        fs::metadata(fixture.root.join("var/tmp")).unwrap().mode() & 0o7777,
        0o1777
    );
    drop(authority);
}

#[test]
fn retained_prefix_revalidation_separates_identity_from_link_topology() {
    let fixture = Fixture::new_v3();
    let authority =
        SessionAuthority::acquire(&fixture.root, std::process::id() as libc::pid_t).unwrap();
    let before = identity(authority.prefix.as_raw_fd()).unwrap();
    fs::create_dir(fixture.root.join("post-acquisition-directory")).unwrap();
    let after = identity(authority.prefix.as_raw_fd()).unwrap();
    assert_eq!((before.device, before.inode), (after.device, after.inode));
    assert_ne!(before.nlink, after.nlink);
    authority.revalidate().unwrap();
}

#[test]
fn persistent_user_home_does_not_expand_controller_cleanup_scope() {
    let fixture = Fixture::new_v3();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    controller
        .prepare_user_home(crate::preinit_user_home::UserHomePlan {
            login: b"cohort-user".to_vec(),
            targets: std::array::from_fn(|index| {
                (index == 0).then(|| b"/Volumes/SystemRoot/home/cohort-user".to_vec())
            }),
            owner_uid: unsafe { libc::geteuid() },
            owner_gid: unsafe { libc::getegid() },
            shared_mode: 0o777,
            user_mode: 0o755,
        })
        .unwrap();
    controller.finish().unwrap();
    assert!(fixture
        .root
        .join("Users/cohort-user/LinuxHome")
        .is_symlink());
}

fn connect(path: &Path) -> OwnedFd {
    let fd = unsafe { libc::socket(libc::AF_UNIX, libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC, 0) };
    assert!(fd >= 0);
    let fd = unsafe { OwnedFd::from_raw_fd(fd) };
    let mut address = MaybeUninit::<libc::sockaddr_un>::zeroed();
    let address_ptr = address.as_mut_ptr();
    let name = path.as_os_str().as_bytes();
    assert!(name.len() < unsafe { (*address_ptr).sun_path.len() });
    unsafe {
        (*address_ptr).sun_family = libc::AF_UNIX as libc::sa_family_t;
        ptr::copy_nonoverlapping(
            name.as_ptr().cast::<c_char>(),
            (*address_ptr).sun_path.as_mut_ptr(),
            name.len(),
        );
    }
    let length = (size_of::<libc::sa_family_t>() + name.len() + 1) as libc::socklen_t;
    assert_eq!(
        unsafe { libc::connect(fd.as_raw_fd(), address_ptr.cast(), length) },
        0
    );
    fd
}

#[test]
fn c_abi_routes_one_idempotent_create_through_retained_service() {
    let fixture = Fixture::new();
    publish_runtime_lower_binding(&fixture);
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(&mut controller) },
        0
    );
    let mut request = GuestTransactionWireRequest {
        transaction_id: [42; 16],
        operation: 1,
        flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
        mode: 0o600,
        source_length: b"var/tmp/rpc-created".len() as u16,
        destination_length: 0,
        source: [0; GUEST_TRANSACTION_PATH_CAPACITY],
        destination: [0; GUEST_TRANSACTION_PATH_CAPACITY],
    };
    request.transaction_id[..8].copy_from_slice(
        &controller
            .guest_namespace
            .as_ref()
            .unwrap()
            .generation()
            .to_ne_bytes(),
    );
    request.source[..usize::from(request.source_length)].copy_from_slice(b"var/tmp/rpc-created");
    for _ in 0..2 {
        let mut result = GuestTransactionWireResult {
            result: -1,
            disposition: 0,
            device: 0,
            inode: 0,
            created_fd: -1,
            reserved: 0,
        };
        assert_eq!(
            unsafe {
                darling_lifecycle_guest_namespace_transaction(
                    &mut controller,
                    &request,
                    &mut result,
                )
            },
            0
        );
        assert_eq!(result.result, 0);
        assert_eq!(result.disposition, 1);
        assert!(result.created_fd >= 0);
        let fd = unsafe { OwnedFd::from_raw_fd(result.created_fd) };
        assert_eq!(identity(fd.as_raw_fd()).unwrap().inode, result.inode);
    }
    assert!(fixture.root.join("var/tmp/rpc-created").exists());
    controller.finish().unwrap();
}

#[test]
fn c_abi_rotates_only_var_run_and_preserves_var_tmp() {
    let fixture = Fixture::new_v3();
    let sentinel = fixture.root.join("var/tmp/preinit-sentinel");
    fs::write(&sentinel, b"persistent").unwrap();
    let before = fs::metadata(&sentinel).unwrap();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_cohort_prepare_var_run(&mut controller) },
        0
    );
    let after = fs::metadata(&sentinel).unwrap();
    assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
    assert_eq!(fs::read(&sentinel).unwrap(), b"persistent");
    let first = fs::metadata(fixture.root.join("var/run")).unwrap();
    assert_eq!(
        unsafe { darling_lifecycle_cohort_prepare_var_run(&mut controller) },
        0
    );
    let repeated = fs::metadata(fixture.root.join("var/run")).unwrap();
    assert_eq!((first.dev(), first.ino()), (repeated.dev(), repeated.ino()));
    controller.finish().unwrap();

    let (mut reused, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_cohort_prepare_var_run(&mut reused) },
        0
    );
    let next = fs::metadata(fixture.root.join("var/run")).unwrap();
    assert_ne!((first.dev(), first.ino()), (next.dev(), next.ino()));
    reused.finish().unwrap();
}

#[test]
fn interrupted_var_run_rotation_closes_admission_and_retains_recovery() {
    let fixture = Fixture::new_v3();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    crate::preinit_var_run::inject_fault(2);
    assert!(controller.prepare_var_run().is_err());
    assert!(controller.preinit_var_run_recovery_pending());
    let retained_parent = controller.preinit_var_parent.as_ref().unwrap();
    let retained_identity = identity(retained_parent.as_raw_fd()).unwrap();
    fs::rename(fixture.root.join("var"), fixture.root.join("var-retained")).unwrap();
    fs::create_dir(fixture.root.join("var")).unwrap();
    assert_eq!(
        identity(retained_parent.as_raw_fd()).unwrap(),
        retained_identity
    );
    assert!(!controller.guest_namespace.as_ref().unwrap().is_active());
}

#[test]
fn var_run_phase_rejects_legacy_prefix_state_before_mutation() {
    let fixture = Fixture::new();
    let public = fs::metadata(fixture.root.join("var/run")).unwrap();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_cohort_prepare_var_run(&mut controller) },
        -1
    );
    let after = fs::metadata(fixture.root.join("var/run")).unwrap();
    assert_eq!((public.dev(), public.ino()), (after.dev(), after.ino()));
    assert!(!fixture.root.join(".darling-var-run-state-v1").exists());
    controller.finish().unwrap();
}

#[test]
fn lower_root_ancestor_symlink_is_rejected_by_rust_acquisition() {
    let fixture = Fixture::new();
    publish_runtime_lower_binding(&fixture);
    let ancestor = fixture.root.join("libexec");
    let retained = fixture.root.join("libexec-retained");
    fs::rename(&ancestor, &retained).unwrap();
    std::os::unix::fs::symlink(&retained, &ancestor).unwrap();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(&mut controller) },
        -1
    );
    controller.finish().unwrap();
}

#[test]
fn lower_root_with_unrelated_deployment_identity_is_rejected() {
    let fixture = Fixture::new();
    publish_runtime_lower_binding(&fixture);
    let controller_path = fixture.root.join("bin/darlingserver");
    fs::remove_file(&controller_path).unwrap();
    fs::write(controller_path, b"forged executable").unwrap();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(&mut controller) },
        -1
    );
    controller.finish().unwrap();
}

#[test]
fn binding_replacement_after_configuration_refuses_before_mutation() {
    let fixture = Fixture::new();
    publish_runtime_lower_binding(&fixture);
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(&mut controller) },
        0
    );
    let binding = fixture.root.join(std::ffi::OsStr::from_bytes(
        crate::runtime_lower_binding::NAME,
    ));
    fs::rename(&binding, fixture.root.join("binding.retained")).unwrap();
    fs::write(&binding, b"replacement").unwrap();
    fs::set_permissions(&binding, fs::Permissions::from_mode(0o600)).unwrap();

    let mut request = GuestTransactionWireRequest {
        transaction_id: [44; 16],
        operation: 1,
        flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
        mode: 0o600,
        source_length: b"var/tmp/must-not-exist".len() as u16,
        destination_length: 0,
        source: [0; GUEST_TRANSACTION_PATH_CAPACITY],
        destination: [0; GUEST_TRANSACTION_PATH_CAPACITY],
    };
    request.transaction_id[..8].copy_from_slice(
        &controller
            .guest_namespace
            .as_ref()
            .unwrap()
            .generation()
            .to_ne_bytes(),
    );
    request.source[..usize::from(request.source_length)].copy_from_slice(b"var/tmp/must-not-exist");
    let mut result = GuestTransactionWireResult {
        result: 0,
        disposition: 0,
        device: 0,
        inode: 0,
        created_fd: -1,
        reserved: 0,
    };
    assert_eq!(
        unsafe {
            darling_lifecycle_guest_namespace_transaction(&mut controller, &request, &mut result)
        },
        -1
    );
    assert!(!fixture.root.join("var/tmp/must-not-exist").exists());
    controller.finish().unwrap();
}

#[test]
fn transaction_c_abi_serializes_concurrent_guest_writers() {
    fn assert_sync<T: Sync>() {}
    assert_sync::<CohortController>();
    let fixture = Fixture::new();
    publish_runtime_lower_binding(&fixture);
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(&mut controller) },
        0
    );
    let generation = controller.guest_namespace.as_ref().unwrap().generation();
    let controller_address = (&mut controller as *mut CohortController) as usize;
    std::thread::scope(|scope| {
        for index in 1u8..=8 {
            scope.spawn(move || {
                let path = format!("var/tmp/concurrent-{index}");
                let mut request = GuestTransactionWireRequest {
                    transaction_id: [index; 16],
                    operation: 1,
                    flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
                    mode: 0o600,
                    source_length: path.len() as u16,
                    destination_length: 0,
                    source: [0; GUEST_TRANSACTION_PATH_CAPACITY],
                    destination: [0; GUEST_TRANSACTION_PATH_CAPACITY],
                };
                request.transaction_id[..8].copy_from_slice(&generation.to_ne_bytes());
                request.source[..path.len()].copy_from_slice(path.as_bytes());
                let mut result = GuestTransactionWireResult {
                    result: -1,
                    disposition: 0,
                    device: 0,
                    inode: 0,
                    created_fd: -1,
                    reserved: 0,
                };
                let controller = controller_address as *mut CohortController;
                assert_eq!(
                    unsafe {
                        darling_lifecycle_guest_namespace_transaction(
                            controller,
                            &request,
                            &mut result,
                        )
                    },
                    0
                );
                assert_eq!(result.result, 0);
                drop(unsafe { OwnedFd::from_raw_fd(result.created_fd) });
            });
        }
    });
    for index in 1..=8 {
        assert!(fixture
            .root
            .join(format!("var/tmp/concurrent-{index}"))
            .exists());
    }
    controller.finish().unwrap();
}

#[test]
fn transaction_recovery_obligation_forces_forensic_finish() {
    let fixture = Fixture::new();
    publish_runtime_lower_binding(&fixture);
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(&mut controller) },
        0
    );
    let created = fixture.root.join("var/tmp/replaced-create");
    let retained = fixture.root.join("var/tmp/replaced-create.retained");
    {
        let mut slot = controller.guest_transactions.lock().unwrap();
        let service = slot.as_mut().unwrap();
        let hook_created = created.clone();
        let hook_retained = retained.clone();
        service.set_test_hook(move |checkpoint| {
            if checkpoint == crate::guest_namespace_transaction::TestCheckpoint::AfterMutation {
                fs::rename(&hook_created, &hook_retained).unwrap();
                fs::write(&hook_created, b"replacement").unwrap();
            }
        });
    }
    let mut request = GuestTransactionWireRequest {
        transaction_id: [43; 16],
        operation: 1,
        flags: libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
        mode: 0o600,
        source_length: b"var/tmp/replaced-create".len() as u16,
        destination_length: 0,
        source: [0; GUEST_TRANSACTION_PATH_CAPACITY],
        destination: [0; GUEST_TRANSACTION_PATH_CAPACITY],
    };
    request.transaction_id[..8].copy_from_slice(
        &controller
            .guest_namespace
            .as_ref()
            .unwrap()
            .generation()
            .to_ne_bytes(),
    );
    request.source[..usize::from(request.source_length)]
        .copy_from_slice(b"var/tmp/replaced-create");
    let mut result = GuestTransactionWireResult {
        result: 0,
        disposition: 0,
        device: 0,
        inode: 0,
        created_fd: -1,
        reserved: 0,
    };
    assert_eq!(
        unsafe {
            darling_lifecycle_guest_namespace_transaction(&mut controller, &request, &mut result)
        },
        0
    );
    assert_eq!(result.disposition, 4);
    assert_eq!(result.created_fd, -1);
    let raw = Box::into_raw(Box::new(controller));
    assert_eq!(
        unsafe { darling_lifecycle_cohort_finish(raw) },
        RECOVERY_PENDING_STATUS
    );
    assert_eq!(
        unsafe { darling_lifecycle_cohort_finish(raw) },
        RECOVERY_PENDING_STATUS
    );
    assert_eq!(
        unsafe { darling_lifecycle_cohort_abandon(raw) },
        ABANDON_PENDING_STATUS
    );
    let mut controller = unsafe { Box::from_raw(raw) };
    assert!(controller.guest_transaction_recovery_pending());
    assert_eq!(fs::read(&created).unwrap(), b"replacement");
    assert_eq!(fs::read(&retained).unwrap(), b"");
    assert!(fixture.root.join(".lc-v1.sock").exists());
    controller.abandon_after_revocation().unwrap();
    controller
        .discard_forensic_transaction_sidecar_for_contract()
        .unwrap();
}

#[test]
fn dserver_log_is_opened_fd_relative_and_persists_after_clean_finish() {
    let fixture = Fixture::new();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let log = controller.take_dserver_log().unwrap();
    write_all(log.as_fd(), b"cohort-main-log\n").unwrap();
    let descriptor_identity = identity(log.as_raw_fd()).unwrap();
    let path = fixture.root.join("private/var/log/dserver.log");
    let metadata = fs::metadata(&path).unwrap();
    assert_eq!(metadata.dev(), descriptor_identity.device);
    assert_eq!(metadata.ino(), descriptor_identity.inode);
    assert_eq!(metadata.mode() & 0o777, 0o644);
    controller.finish().unwrap();
    assert_eq!(fs::read(&path).unwrap(), b"cohort-main-log\n");
    assert!(!fixture
        .root
        .join("private/var/log/dserver-auxlog.txt")
        .exists());
}

#[test]
fn dserver_log_replacement_is_preserved_and_finish_fails_closed() {
    let fixture = Fixture::new();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let log = controller.take_dserver_log().unwrap();
    let original = identity(log.as_raw_fd()).unwrap();
    let directory = fixture.root.join("private/var/log");
    let path = directory.join("dserver.log");
    let retained = directory.join("dserver.log.retained");
    fs::rename(&path, &retained).unwrap();
    fs::write(&path, b"replacement\n").unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();
    assert!(controller.finish().is_err());
    assert_eq!(fs::read(&path).unwrap(), b"replacement\n");
    let metadata = fs::metadata(&retained).unwrap();
    assert_eq!(
        (metadata.dev(), metadata.ino()),
        (original.device, original.inode)
    );
}

#[test]
fn c_abi_admission_query_is_bound_to_live_guest_authority() {
    assert!(!unsafe { darling_lifecycle_cohort_admission_open(std::ptr::null_mut()) });
    let fixture = Fixture::new();
    let (controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let mut sockets = [0; 2];
    assert_eq!(
        unsafe {
            libc::socketpair(
                libc::AF_UNIX,
                libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                0,
                sockets.as_mut_ptr(),
            )
        },
        0
    );
    controller
        .guest_namespace
        .as_ref()
        .unwrap()
        .send_bootstrap(sockets[0])
        .unwrap();
    let session =
        crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
            .unwrap();
    unsafe {
        libc::close(sockets[0]);
        libc::close(sockets[1]);
    }
    let mutation = session.authorize().unwrap();
    let pointer = Box::into_raw(Box::new(controller));
    assert!(unsafe { darling_lifecycle_cohort_admission_open(pointer) });
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 1);
    assert!(!unsafe { darling_lifecycle_cohort_admission_open(pointer) });
    drop(mutation);
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 0);
}

#[test]
fn guest_mutation_drain_pending_preserves_owning_controller_for_retry() {
    let fixture = Fixture::new();
    let (controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let mut sockets = [0; 2];
    assert_eq!(
        unsafe {
            libc::socketpair(
                libc::AF_UNIX,
                libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                0,
                sockets.as_mut_ptr(),
            )
        },
        0
    );
    controller
        .guest_namespace
        .as_ref()
        .unwrap()
        .send_bootstrap(sockets[0])
        .unwrap();
    let session =
        crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
            .unwrap();
    unsafe {
        libc::close(sockets[0]);
        libc::close(sockets[1]);
    }
    let mutation = session.authorize().unwrap();
    let control_path = controller.control_test_path.clone();
    let pending = match controller.finish() {
        Err(CohortFinishError::DrainPending { controller, .. }) => controller,
        other => panic!("expected owning drain pending, got {other:?}"),
    };
    assert!(control_path.exists());
    assert!(matches!(
        session.authorize(),
        Err(crate::guest_namespace_authority::AuthorityError::Revoked)
    ));
    drop(mutation);
    pending.finish().unwrap();
    assert!(!control_path.exists());
}

#[test]
fn c_abi_drain_pending_preserves_exact_pointer_for_retry() {
    let fixture = Fixture::new();
    let (controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let mut sockets = [0; 2];
    assert_eq!(
        unsafe {
            libc::socketpair(
                libc::AF_UNIX,
                libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                0,
                sockets.as_mut_ptr(),
            )
        },
        0
    );
    controller
        .guest_namespace
        .as_ref()
        .unwrap()
        .send_bootstrap(sockets[0])
        .unwrap();
    let session =
        crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
            .unwrap();
    unsafe {
        libc::close(sockets[0]);
        libc::close(sockets[1]);
    }
    let mutation = session.authorize().unwrap();
    let pointer = Box::into_raw(Box::new(controller));
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 1);
    assert!(matches!(
        session.authorize(),
        Err(crate::guest_namespace_authority::AuthorityError::Revoked)
    ));
    drop(mutation);
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 0);
}

fn assert_unknown_controller_command_preserves(command: u8) {
    let fixture = Fixture::new();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let path = controller.control_test_path.clone();
    let before = fs::symlink_metadata(&path).unwrap();
    assert_eq!(
        unsafe {
            libc::send(
                controller.shutdown.as_raw_fd(),
                (&command as *const u8).cast(),
                1,
                libc::MSG_NOSIGNAL,
            )
        },
        1
    );
    let (authority, result, forensic_preserve) = controller.thread.take().unwrap().join().unwrap();
    assert!(result.is_ok());
    assert!(forensic_preserve);
    drop(authority);
    let after = fs::symlink_metadata(&path).unwrap();
    assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
}

#[test]
fn accumulated_eventfd_values_four_and_six_are_unknown_and_preserve() {
    assert_unknown_controller_command_preserves(4);
    assert_unknown_controller_command_preserves(6);
}

#[test]
fn missing_preserve_ack_never_reenables_cleanup() {
    let fixture = Fixture::new();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let path = controller.control_test_path.clone();
    let before = fs::symlink_metadata(&path).unwrap();
    controller.forensic_preserve_requested = true;
    assert_eq!(
        unsafe { libc::shutdown(controller.shutdown.as_raw_fd(), libc::SHUT_RDWR) },
        0
    );
    assert!(controller.request_forensic_preserve().is_err());
    assert!(controller.request_cleanup().is_err());
    let (authority, _, forensic_preserve) = controller.thread.take().unwrap().join().unwrap();
    assert!(forensic_preserve);
    drop(authority);
    let after = fs::symlink_metadata(&path).unwrap();
    assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
}

#[test]
fn post_send_cleanup_ack_loss_only_reaps_and_never_abandons() {
    let fixture = Fixture::new();
    let (mut controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let path = controller.control_test_path.clone();
    controller.lose_cleanup_ack_after_receive = true;
    let pointer = Box::into_raw(Box::new(controller));
    assert_eq!(
        unsafe { darling_lifecycle_cohort_finish(pointer) },
        CLEANUP_PENDING_STATUS
    );
    assert_eq!(
        unsafe { (*pointer).cleanup_phase },
        CleanupPhase::CleanupCommitted
    );
    // CLEANUP is committed. A raw abandon attempt is refused while
    // preserving the exact owning pointer and cannot select forensics.
    assert_eq!(
        unsafe { darling_lifecycle_cohort_abandon(pointer) },
        ABANDON_PENDING_STATUS
    );
    assert_eq!(
        unsafe { (*pointer).cleanup_phase },
        CleanupPhase::CleanupCommitted
    );
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 0);
    assert!(!path.exists());
}

#[test]
fn c_abi_abandon_stops_worker_without_destructive_cleanup() {
    let fixture = Fixture::new();
    let (controller, listener) =
        CohortController::start_for_test(&fixture.root, std::process::id() as _).unwrap();
    drop(listener);
    let control_path = controller.control_test_path.clone();
    let mut sockets = [0; 2];
    assert_eq!(
        unsafe {
            libc::socketpair(
                libc::AF_UNIX,
                libc::SOCK_SEQPACKET | libc::SOCK_CLOEXEC,
                0,
                sockets.as_mut_ptr(),
            )
        },
        0
    );
    controller
        .guest_namespace
        .as_ref()
        .unwrap()
        .send_bootstrap(sockets[0])
        .unwrap();
    let session =
        crate::guest_namespace_authority::SessionCapabilities::receive_required(sockets[1])
            .unwrap();
    unsafe {
        libc::close(sockets[0]);
        libc::close(sockets[1]);
    }
    let mutation = session.authorize().unwrap();
    let mut controller = controller;
    controller.abandon_fault = Some(AbandonFault::WaitpidError);
    let pointer = Box::into_raw(Box::new(controller));
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(pointer) }, 1);
    assert_eq!(
        unsafe { darling_lifecycle_cohort_abandon(pointer) },
        ABANDON_PENDING_STATUS
    );
    assert!(control_path.exists());
    assert!(matches!(
        session.authorize(),
        Err(crate::guest_namespace_authority::AuthorityError::Revoked)
    ));
    assert_eq!(unsafe { darling_lifecycle_cohort_abandon(pointer) }, 0);
    assert!(control_path.exists());
    assert!(matches!(
        session.authorize(),
        Err(crate::guest_namespace_authority::AuthorityError::Revoked)
    ));
    drop(mutation);
    drop(session);
}

#[test]
fn real_process_abandon_faults_preserve_worker_until_retry_reaps() {
    for fault in [
        AbandonFault::KillError,
        AbandonFault::PidfdTimeout,
        AbandonFault::WaitpidEintr,
        AbandonFault::WaitpidZero,
        AbandonFault::WaitpidError,
    ] {
        let child = unsafe { libc::fork() };
        assert!(child >= 0);
        if child == 0 {
            loop {
                unsafe { libc::pause() };
            }
        }
        let worker = ProcessWorker {
            pid: child,
            pidfd: pidfd_open(child).unwrap(),
        };
        assert!(abandon_process_worker(&worker, Some(fault)).is_err());
        assert!(unsafe { libc::fcntl(worker.pidfd.as_raw_fd(), libc::F_GETFD) } >= 0);
        if fault == AbandonFault::KillError {
            assert_eq!(unsafe { libc::kill(child, 0) }, 0);
        }
        abandon_process_worker(&worker, None).unwrap();
        let mut status = 0;
        assert_eq!(
            unsafe { libc::waitpid(child, &mut status, libc::WNOHANG) },
            -1
        );
        assert_eq!(
            io::Error::last_os_error().raw_os_error(),
            Some(libc::ECHILD)
        );
    }
}

#[test]
fn dserver_log_symlink_is_rejected_before_writer_transfer() {
    let fixture = Fixture::new();
    let directory = fixture.root.join("private/var/log");
    fs::create_dir_all(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
    fs::write(directory.join("outside"), b"preserved\n").unwrap();
    symlink("outside", directory.join("dserver.log")).unwrap();
    assert!(CohortController::start_for_test(&fixture.root, std::process::id() as _).is_err());
    assert_eq!(fs::read(directory.join("outside")).unwrap(), b"preserved\n");
}

#[test]
fn dserver_log_fifo_is_rejected_without_opening_a_writer() {
    let fixture = Fixture::new();
    let directory = fixture.root.join("private/var/log");
    fs::create_dir_all(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
    let fifo = CString::new(directory.join("dserver.log").as_os_str().as_bytes()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o644) }, 0);
    let started = Instant::now();
    assert!(CohortController::start_for_test(&fixture.root, std::process::id() as _).is_err());
    assert!(started.elapsed() < Duration::from_secs(1));
    assert!(fs::symlink_metadata(directory.join("dserver.log"))
        .unwrap()
        .file_type()
        .is_fifo());
}

#[test]
fn dserver_log_creation_normalizes_aggressive_umask_inode_bound() {
    const CHILD: &str = "DARLING_LIFECYCLE_UMASK_TEST_CHILD";
    if std::env::var_os(CHILD).is_none() {
        let status = Command::new(std::env::current_exe().unwrap())
                .args([
                    "--exact",
                    "cohort_routing::tests::dserver_log_creation_normalizes_aggressive_umask_inode_bound",
                    "--nocapture",
                ])
                .env(CHILD, "1")
                .status()
                .unwrap();
        assert!(status.success());
        return;
    }
    let fixture = Fixture::new();
    let previous = unsafe { libc::umask(0o077) };
    let result = CohortController::start_for_test(&fixture.root, std::process::id() as _);
    unsafe { libc::umask(previous) };
    let (mut controller, listener) = result.unwrap();
    drop(listener);
    let log = controller.take_dserver_log().unwrap();
    assert_eq!(identity(log.as_raw_fd()).unwrap().mode & 0o777, 0o644);
    controller.finish().unwrap();
    assert_eq!(
        fs::metadata(fixture.root.join("private/var/log/dserver.log"))
            .unwrap()
            .mode()
            & 0o777,
        0o644
    );
}

#[test]
fn dserver_log_created_inode_rollback_is_exact_and_replacement_safe() {
    let fixture = Fixture::new();
    let directory = fixture.root.join("private/var/log");
    fs::create_dir_all(&directory).unwrap();
    fs::set_permissions(&directory, fs::Permissions::from_mode(0o755)).unwrap();
    let parent = open_directory_chain(
        open_prefix(&fixture.root).unwrap().as_fd(),
        DSERVER_LOG_PARENT,
    )
    .unwrap();
    let created = openat(
        parent.as_fd(),
        DSERVER_LOG_NAME,
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
        0o600,
    )
    .unwrap();
    let created_identity = identity(created.as_raw_fd()).unwrap();
    rollback_created_log(parent.as_fd(), DSERVER_LOG_NAME, created_identity).unwrap();
    assert!(!directory.join("dserver.log").exists());

    let original = openat(
        parent.as_fd(),
        DSERVER_LOG_NAME,
        libc::O_WRONLY | libc::O_CREAT | libc::O_EXCL | libc::O_CLOEXEC,
        0o600,
    )
    .unwrap();
    let original_identity = identity(original.as_raw_fd()).unwrap();
    fs::rename(directory.join("dserver.log"), directory.join("retained")).unwrap();
    fs::write(directory.join("dserver.log"), b"replacement\n").unwrap();
    assert!(rollback_created_log(parent.as_fd(), DSERVER_LOG_NAME, original_identity).is_err());
    assert_eq!(
        fs::read(directory.join("dserver.log")).unwrap(),
        b"replacement\n"
    );
    assert_eq!(
        fs::metadata(directory.join("retained")).unwrap().ino(),
        original_identity.inode
    );
}

#[test]
fn dserver_log_post_create_failure_removes_exact_inode() {
    let fixture = Fixture::new();
    let mut authority = SessionAuthority::acquire(&fixture.root, std::process::id() as _).unwrap();
    authority.fail_log_after_create = true;
    assert!(matches!(
        authority.publish_log(),
        Err(CohortError::Protocol("injected post-create log failure"))
    ));
    assert!(!fixture.root.join("private/var/log/dserver.log").exists());
    assert!(authority.log.is_none());
}

#[test]
fn dserver_log_bootstrap_transfers_exact_writer_fd_once() {
    let fixture = Fixture::new();
    let prefix = open_prefix(&fixture.root).unwrap();
    let prefix_argument = CString::new(fixture.root.as_os_str().as_bytes()).unwrap();
    let mut bootstrap = MaybeUninit::<CohortBootstrap>::zeroed();
    let controller = unsafe {
        darling_lifecycle_cohort_start(
            prefix.as_raw_fd(),
            prefix.as_raw_fd(),
            prefix_argument.as_ptr(),
            std::process::id() as _,
            bootstrap.as_mut_ptr(),
        )
    };
    assert!(!controller.is_null());
    let bootstrap = unsafe { bootstrap.assume_init() };
    assert!(bootstrap.darlingserver_fd >= 0);
    assert!(bootstrap.dserver_log_fd >= 0);
    let log = crate::inherited_fd::duplicate_cloexec(bootstrap.dserver_log_fd, 3).unwrap();
    write_all(log.as_fd(), b"ffi-bootstrap-log\n").unwrap();
    let expected = identity(bootstrap.dserver_log_fd).unwrap();
    assert_eq!(unsafe { darling_lifecycle_cohort_finish(controller) }, 0);
    let path = fixture.root.join("private/var/log/dserver.log");
    let metadata = fs::metadata(&path).unwrap();
    assert_eq!(
        (metadata.dev(), metadata.ino()),
        (expected.device, expected.inode)
    );
    assert_eq!(fs::read(&path).unwrap(), b"ffi-bootstrap-log\n");
    unsafe {
        libc::close(bootstrap.darlingserver_fd);
        libc::close(bootstrap.dserver_log_fd);
    }
}

fn request(
    controller: &CohortController,
    kind: CohortEndpoint,
    operation: u16,
    nonce: [u8; 32],
) -> WireResponse {
    let client = connect(&controller.control_test_path);
    let request = WireRequest {
        magic: PROTOCOL_MAGIC,
        version: PROTOCOL_VERSION,
        operation,
        endpoint: kind as u16,
        reserved: 0,
        nonce,
    };
    assert_eq!(
        unsafe {
            libc::send(
                client.as_raw_fd(),
                (&request as *const WireRequest).cast(),
                size_of::<WireRequest>(),
                libc::MSG_NOSIGNAL,
            )
        },
        size_of::<WireRequest>() as isize
    );
    let mut response = MaybeUninit::<WireResponse>::zeroed();
    assert_eq!(
        unsafe {
            libc::recv(
                client.as_raw_fd(),
                response.as_mut_ptr().cast(),
                size_of::<WireResponse>(),
                0,
            )
        },
        size_of::<WireResponse>() as isize
    );
    let response = unsafe { response.assume_init() };
    if operation == OPERATION_PUBLISH && response.status == 0 {
        let decision = WireRequest {
            magic: PROTOCOL_MAGIC,
            version: PROTOCOL_VERSION,
            operation: OPERATION_COMMIT,
            endpoint: kind as u16,
            reserved: 0,
            nonce,
        };
        assert_eq!(
            unsafe {
                libc::send(
                    client.as_raw_fd(),
                    (&decision as *const WireRequest).cast(),
                    size_of::<WireRequest>(),
                    libc::MSG_NOSIGNAL,
                )
            },
            size_of::<WireRequest>() as isize
        );
        let committed = receive_response_only(client.as_raw_fd());
        assert_eq!(committed.status, 0);
        assert_eq!(committed.endpoint, kind as u16);
        assert_eq!(committed.has_fd, 0);
        assert_eq!(committed.phase, RESPONSE_PHASE_COMMIT);
        assert_eq!(committed.error, RESPONSE_ERROR_NONE);
    }
    response
}

fn send_request_only(
    client: RawFd,
    kind: CohortEndpoint,
    operation: u16,
    nonce: [u8; NONCE_BYTES],
) {
    let request = WireRequest {
        magic: PROTOCOL_MAGIC,
        version: PROTOCOL_VERSION,
        operation,
        endpoint: kind as u16,
        reserved: 0,
        nonce,
    };
    assert_eq!(
        unsafe {
            libc::send(
                client,
                (&request as *const WireRequest).cast(),
                size_of::<WireRequest>(),
                libc::MSG_NOSIGNAL,
            )
        },
        size_of::<WireRequest>() as isize
    );
}

fn receive_response_only(client: RawFd) -> WireResponse {
    let mut response = MaybeUninit::<WireResponse>::zeroed();
    assert_eq!(
        unsafe {
            libc::recv(
                client,
                response.as_mut_ptr().cast(),
                size_of::<WireResponse>(),
                0,
            )
        },
        size_of::<WireResponse>() as isize
    );
    unsafe { response.assume_init() }
}

fn wait_until_missing(path: &Path) {
    for _ in 0..100 {
        if fs::symlink_metadata(path).is_err_and(|error| error.kind() == io::ErrorKind::NotFound) {
            return;
        }
        thread::sleep(Duration::from_millis(5));
    }
    panic!(
        "pending publication was not rolled back: {}",
        path.display()
    );
}

#[test]
fn routed_publish_and_retire_are_fd_relative_under_exact_lease() {
    let fixture = Fixture::new();
    let (controller, darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    assert!(darlingserver.as_raw_fd() >= 0);
    let publish = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
    assert_eq!(publish.status, 0);
    assert_eq!(publish.phase, RESPONSE_PHASE_PUBLISH);
    assert_eq!(publish.error, RESPONSE_ERROR_NONE);
    assert_eq!(publish.has_fd, 1);
    assert!(fixture.root.join("var/run/shellspawn.sock").exists());
    let retire = request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce);
    assert_eq!(retire.status, 0);
    assert_eq!(retire.phase, RESPONSE_PHASE_RETIRE);
    assert_eq!(retire.error, RESPONSE_ERROR_NONE);
    assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
    controller.finish().unwrap();
    assert!(!fixture.root.join(".init.pid").exists());
    assert!(!fixture.root.join(".darlingserver.sock").exists());
}

#[test]
fn launchd_exact_socket_runs_publish_transfer_activation_commit_and_recovery_phases() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let endpoint = fixture.root.join("var/tmp/launchd/sock");

    let client = connect(&controller.control_test_path);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Launchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    let pending = receive_response_only(client.as_raw_fd());
    assert_eq!(pending.status, 0);
    assert_eq!(pending.phase, RESPONSE_PHASE_PUBLISH);
    assert_eq!(pending.error, RESPONSE_ERROR_NONE);
    assert_eq!(pending.has_fd, 1);
    assert!(endpoint.exists());

    // Activation failure is represented by an authenticated ABORT.  It
    // must remove the exact pending inode before a retry can publish.
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Launchd,
        OPERATION_ABORT,
        controller.nonce,
    );
    let aborted = receive_response_only(client.as_raw_fd());
    assert_eq!(aborted.phase, RESPONSE_PHASE_ABORT);
    assert_eq!(aborted.error, RESPONSE_ERROR_NONE);
    wait_until_missing(&endpoint);

    let committed = request(
        &controller,
        CohortEndpoint::Launchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    assert_eq!(committed.phase, RESPONSE_PHASE_PUBLISH);
    assert_eq!(committed.error, RESPONSE_ERROR_NONE);
    assert!(endpoint.exists());

    let duplicate = request(
        &controller,
        CohortEndpoint::Launchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    assert_eq!(duplicate.status, -1);
    assert_eq!(duplicate.phase, RESPONSE_PHASE_PUBLISH);
    assert_eq!(duplicate.error, RESPONSE_ERROR_ENDPOINT_EXISTS);
    assert!(endpoint.exists());

    let retired = request(
        &controller,
        CohortEndpoint::Launchd,
        OPERATION_RETIRE,
        controller.nonce,
    );
    assert_eq!(retired.status, 0);
    assert_eq!(retired.phase, RESPONSE_PHASE_RETIRE);
    assert_eq!(retired.error, RESPONSE_ERROR_NONE);
    wait_until_missing(&endpoint);

    let missing = request(
        &controller,
        CohortEndpoint::Launchd,
        OPERATION_RETIRE,
        controller.nonce,
    );
    assert_eq!(missing.status, -1);
    assert_eq!(missing.phase, RESPONSE_PHASE_RETIRE);
    assert_eq!(missing.error, RESPONSE_ERROR_ENDPOINT_MISSING);
    controller.finish().unwrap();
}

fn response_path(response: &WireResponse) -> String {
    let length = usize::from(response.path_len);
    assert!(length > 0 && length < response.path.len());
    assert_eq!(response.path[length], 0);
    std::str::from_utf8(&response.path[..length])
        .unwrap()
        .to_owned()
}

#[test]
fn per_user_launchd_uses_retained_dynamic_directory_and_exact_retirement() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let published = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    assert_eq!(published.status, 0);
    assert_eq!(published.has_fd, 1);
    let guest_path = response_path(&published);
    assert!(guest_path.starts_with("/private/var/tmp/launchd-"));
    assert!(guest_path.ends_with("/sock"));
    let endpoint = fixture.root.join(guest_path.trim_start_matches('/'));
    let directory = endpoint.parent().unwrap().to_owned();
    let endpoint_state = fs::symlink_metadata(&endpoint).unwrap();
    assert!(endpoint_state.file_type().is_socket());
    assert_eq!(endpoint_state.permissions().mode() & 0o777, 0o600);
    let directory_state = fs::symlink_metadata(&directory).unwrap();
    assert!(directory_state.is_dir());
    assert_eq!(directory_state.permissions().mode() & 0o777, 0o700);

    let duplicate = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    assert_eq!(duplicate.status, -1);
    assert!(endpoint.exists());

    let retired = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_RETIRE,
        controller.nonce,
    );
    assert_eq!(retired.status, 0);
    assert!(!endpoint.exists());
    assert!(!directory.exists());
    controller.finish().unwrap();
}

#[test]
fn per_user_dynamic_directory_partial_creation_is_rolled_back() {
    let fixture = Fixture::new();
    let mut authority =
        SessionAuthority::acquire(&fixture.root, unsafe { libc::getpid() }).unwrap();
    authority.dynamic_fault = Some(DynamicPublicationFault::AfterDirectoryCreate);
    let peer = process_identity(unsafe { libc::getpid() }).unwrap().0;
    assert!(matches!(
        authority.publish_key(EndpointKey::PerUser(peer)),
        Err(CohortError::Io("fault(after dynamic directory create)", _))
    ));
    let parent = fixture.root.join("private/var/tmp");
    assert!(fs::read_dir(parent).unwrap().next().is_none());
    authority.cleanup_all().unwrap();
}

#[test]
fn per_user_endpoint_replacement_is_preserved_fail_closed() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let published = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    let endpoint = fixture
        .root
        .join(response_path(&published).trim_start_matches('/'));
    let saved = endpoint.with_extension("saved");
    fs::rename(&endpoint, &saved).unwrap();
    fs::write(&endpoint, b"replacement").unwrap();
    let refused = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_RETIRE,
        controller.nonce,
    );
    assert_eq!(refused.status, -1);
    assert_eq!(fs::read(&endpoint).unwrap(), b"replacement");
    fs::remove_file(&endpoint).unwrap();
    fs::rename(&saved, &endpoint).unwrap();
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_RETIRE,
            controller.nonce,
        )
        .status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn per_user_directory_replacement_is_preserved_fail_closed() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let published = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    let endpoint = fixture
        .root
        .join(response_path(&published).trim_start_matches('/'));
    let directory = endpoint.parent().unwrap().to_owned();
    let saved = directory.with_extension("saved");
    fs::rename(&directory, &saved).unwrap();
    fs::create_dir(&directory).unwrap();
    fs::write(directory.join("replacement"), b"preserve").unwrap();
    let refused = request(
        &controller,
        CohortEndpoint::PerUserLaunchd,
        OPERATION_RETIRE,
        controller.nonce,
    );
    assert_eq!(refused.status, -1);
    assert_eq!(
        fs::read(directory.join("replacement")).unwrap(),
        b"preserve"
    );
    fs::remove_file(directory.join("replacement")).unwrap();
    fs::remove_dir(&directory).unwrap();
    fs::rename(&saved, &directory).unwrap();
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::PerUserLaunchd,
            OPERATION_RETIRE,
            controller.nonce,
        )
        .status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn per_user_pending_eof_removes_socket_and_dynamic_directory() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let client = connect(&controller.control_test_path);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::PerUserLaunchd,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    let pending = receive_response_only(client.as_raw_fd());
    let endpoint = fixture
        .root
        .join(response_path(&pending).trim_start_matches('/'));
    let directory = endpoint.parent().unwrap().to_owned();
    drop(client);
    wait_until_missing(&endpoint);
    wait_until_missing(&directory);
    controller.finish().unwrap();
}

#[test]
fn symlinked_dynamic_ancestor_is_rejected_before_mutation() {
    let fixture = Fixture::new();
    let outside = fixture.root.join("outside");
    fs::create_dir(&outside).unwrap();
    std::os::unix::fs::symlink(&outside, fixture.root.join("private")).unwrap();
    assert!(SessionAuthority::acquire(&fixture.root, 4242).is_err());
    assert!(fs::read_dir(outside).unwrap().next().is_none());
}

#[test]
fn pending_publication_rolls_back_on_client_eof_before_adoption() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let client = connect(&controller.control_test_path);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Shellspawn,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    let response = receive_response_only(client.as_raw_fd());
    assert_eq!(response.status, 0);
    drop(client);
    wait_until_missing(&fixture.root.join("var/run/shellspawn.sock"));
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        )
        .status,
        0
    );
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::Shellspawn,
            OPERATION_RETIRE,
            controller.nonce,
        )
        .status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn pending_publication_rolls_back_on_explicit_adoption_abort() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let client = connect(&controller.control_test_path);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Shellspawn,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    assert_eq!(receive_response_only(client.as_raw_fd()).status, 0);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Shellspawn,
        OPERATION_ABORT,
        controller.nonce,
    );
    drop(client);
    wait_until_missing(&fixture.root.join("var/run/shellspawn.sock"));
    controller.finish().unwrap();
}

#[test]
fn response_delivery_failure_cannot_leave_pending_publication() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let client = connect(&controller.control_test_path);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Shellspawn,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    unsafe { libc::shutdown(client.as_raw_fd(), libc::SHUT_RDWR) };
    drop(client);
    wait_until_missing(&fixture.root.join("var/run/shellspawn.sock"));
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        )
        .status,
        0
    );
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::Shellspawn,
            OPERATION_RETIRE,
            controller.nonce,
        )
        .status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn final_ack_delivery_failure_preserves_committed_publication() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let client = connect(&controller.control_test_path);
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Shellspawn,
        OPERATION_PUBLISH,
        controller.nonce,
    );
    assert_eq!(receive_response_only(client.as_raw_fd()).status, 0);

    // Make the final acknowledgement undeliverable while preserving the
    // write side used for the irrevocable COMMIT.
    assert_eq!(
        unsafe { libc::shutdown(client.as_raw_fd(), libc::SHUT_RD) },
        0
    );
    send_request_only(
        client.as_raw_fd(),
        CohortEndpoint::Shellspawn,
        OPERATION_COMMIT,
        controller.nonce,
    );
    drop(client);

    let endpoint = fixture.root.join("var/run/shellspawn.sock");
    assert!(endpoint.exists());
    assert_ne!(
        request(
            &controller,
            CohortEndpoint::Shellspawn,
            OPERATION_PUBLISH,
            controller.nonce,
        )
        .status,
        0,
        "a lost final ACK must not make the committed endpoint publishable again"
    );
    assert_eq!(
        request(
            &controller,
            CohortEndpoint::Shellspawn,
            OPERATION_RETIRE,
            controller.nonce,
        )
        .status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn wrong_nonce_and_unknown_endpoint_fail_closed() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let wrong = request(&controller, CohortEndpoint::Shellspawn, 1, [0x55; 32]);
    assert_ne!(wrong.status, 0);
    assert_eq!(wrong.phase, RESPONSE_PHASE_REQUEST);
    assert_eq!(wrong.error, RESPONSE_ERROR_PROTOCOL);
    assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
    let unknown_operation = request(
        &controller,
        CohortEndpoint::Shellspawn,
        99,
        controller.nonce,
    );
    assert_ne!(unknown_operation.status, 0);
    assert_eq!(unknown_operation.phase, RESPONSE_PHASE_REQUEST);
    assert_eq!(unknown_operation.error, RESPONSE_ERROR_PROTOCOL);
    controller.finish().unwrap();
}

#[test]
fn malformed_request_flood_does_not_consume_authority_lifetime() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    for _ in 0..=MAX_REJECTED_REQUESTS_PER_SLICE {
        let rejected = request(
            &controller,
            CohortEndpoint::Shellspawn,
            1,
            [0x55; NONCE_BYTES],
        );
        assert_ne!(rejected.status, 0);
    }
    let published = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
    assert_eq!(published.status, 0);
    assert_eq!(published.has_fd, 1);
    assert_eq!(
        request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce,).status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn dead_endpoint_owner_transitions_to_exact_republication() {
    let fixture = Fixture::new();
    let mut authority =
        SessionAuthority::acquire(&fixture.root, unsafe { libc::getpid() }).unwrap();
    let first_listener = authority.publish(CohortEndpoint::Shellspawn).unwrap();
    let first_identity = identity(first_listener.as_raw_fd()).unwrap();
    let mut owner = Command::new("/bin/sh")
        .args(["-c", "exec sleep 60"])
        .spawn()
        .unwrap();
    let owner_pid = owner.id() as libc::pid_t;
    let owner_identity = process_identity(owner_pid).unwrap().0;
    let owner_pidfd = unsafe { libc::syscall(libc::SYS_pidfd_open, owner_pid, 0) as c_int };
    assert!(owner_pidfd >= 0);
    authority.endpoint_owners.insert(
        EndpointKey::Static(CohortEndpoint::Shellspawn),
        PeerAuthority {
            identity: owner_identity,
            _process: unsafe { OwnedFd::from_raw_fd(owner_pidfd) },
        },
    );
    owner.kill().unwrap();
    owner.wait().unwrap();
    assert_eq!(
        owner_process_state(
            authority
                .endpoint_owners
                .get(&EndpointKey::Static(CohortEndpoint::Shellspawn))
                .unwrap()
                ._process
                .as_raw_fd()
        )
        .unwrap(),
        OwnerProcessState::Gone
    );
    drop(first_listener);
    let second_listener = authority.publish(CohortEndpoint::Shellspawn).unwrap();
    let second_identity = identity(second_listener.as_raw_fd()).unwrap();
    assert_ne!(first_identity.inode_key(), second_identity.inode_key());
    assert!(!authority
        .endpoint_owners
        .contains_key(&EndpointKey::Static(CohortEndpoint::Shellspawn)));
    authority.cleanup_all().unwrap();
}

#[test]
fn lock_contender_cannot_mutate_existing_inode_before_lease() {
    let fixture = Fixture::new();
    let authority = SessionAuthority::acquire(&fixture.root, unsafe { libc::getpid() }).unwrap();
    let lock_path = fixture.root.join(".lifecycle.lock");
    let before = fs::metadata(&lock_path).unwrap();
    let contender_root = fixture.root.clone();
    let contender = thread::spawn(move || SessionAuthority::acquire(&contender_root, 4343));
    assert!(matches!(
        contender.join().unwrap(),
        Err(CohortError::LockBusy)
    ));
    let after = fs::metadata(&lock_path).unwrap();
    assert_eq!(
        (
            before.dev(),
            before.ino(),
            before.ctime(),
            before.ctime_nsec()
        ),
        (after.dev(), after.ino(), after.ctime(), after.ctime_nsec())
    );
    drop(authority);
}

#[test]
fn retained_prefix_state_rejects_in_place_mutation() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let state = fixture.root.join(".darling-prefix-state-v2");
    let original = fs::read(&state).unwrap();
    let before = fs::metadata(&state).unwrap();
    fs::write(&state, b"privileged-eunion\n").unwrap();
    let after = fs::metadata(&state).unwrap();
    assert_eq!((before.dev(), before.ino()), (after.dev(), after.ino()));
    let rejected = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
    assert_ne!(rejected.status, 0);
    assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
    fs::write(&state, original).unwrap();
    controller.finish().unwrap();
}

#[test]
fn retained_prefix_fd_does_not_reopen_proc_path() {
    let fixture = Fixture::new();
    let prefix = open_prefix(&fixture.root).unwrap();
    let proc_argument = format!("/proc/self/fd/{}", prefix.as_raw_fd());
    let authority =
        SessionAuthority::acquire_from_fd(prefix.as_raw_fd(), proc_argument.as_bytes(), unsafe {
            libc::getpid()
        })
        .unwrap();
    assert_eq!(authority.prefix_argument, proc_argument.as_bytes());
    drop(authority);
}

#[test]
fn controller_creates_missing_endpoint_parents_under_retained_lease() {
    let fixture = Fixture::new();
    fs::remove_dir_all(fixture.root.join("var")).unwrap();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let run = fs::metadata(fixture.root.join("var/run")).unwrap();
    let launchd = fs::metadata(fixture.root.join("var/tmp/launchd")).unwrap();
    assert_eq!(run.mode() & 0o7777, 0o755);
    assert_eq!(launchd.mode() & 0o7777, 0o700);
    controller.finish().unwrap();
}

#[test]
fn retained_init_pid_replacement_is_preserved_on_finish() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let init_pid = fixture.root.join(".init.pid");
    fs::rename(&init_pid, fixture.root.join(".init.pid.original")).unwrap();
    fs::write(&init_pid, b"999999\n").unwrap();
    fs::set_permissions(&init_pid, fs::Permissions::from_mode(0o600)).unwrap();
    assert!(controller.finish().is_err());
    assert_eq!(fs::read(&init_pid).unwrap(), b"999999\n");
}

#[test]
fn connected_client_without_request_has_bounded_unwind() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let _stalled = connect(&controller.control_test_path);
    thread::sleep(Duration::from_millis(20));
    let started = Instant::now();
    controller.finish().unwrap();
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[test]
fn pre_thread_authority_drop_rolls_back_published_namespace() {
    let fixture = Fixture::new();
    {
        let mut authority =
            SessionAuthority::acquire(&fixture.root, unsafe { libc::getpid() }).unwrap();
        drop(authority.publish(CohortEndpoint::DarlingServer).unwrap());
        drop(authority.publish(CohortEndpoint::Control).unwrap());
        assert!(fixture.root.join(".init.pid").exists());
        assert!(fixture.root.join(".darlingserver.sock").exists());
        assert!(fixture.root.join(".lc-v1.sock").exists());
    }
    assert!(!fixture.root.join(".init.pid").exists());
    assert!(!fixture.root.join(".darlingserver.sock").exists());
    assert!(!fixture.root.join(".lc-v1.sock").exists());
}

#[test]
fn split_lock_is_rejected_before_endpoint_mutation() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let original = fs::metadata(fixture.root.join(".lifecycle.lock")).unwrap();
    fs::rename(
        fixture.root.join(".lifecycle.lock"),
        fixture.root.join(".lifecycle.lock.old"),
    )
    .unwrap();
    fs::write(fixture.root.join(".lifecycle.lock"), b"").unwrap();
    fs::set_permissions(
        fixture.root.join(".lifecycle.lock"),
        fs::Permissions::from_mode(0o600),
    )
    .unwrap();
    let replacement = fs::metadata(fixture.root.join(".lifecycle.lock")).unwrap();
    assert_ne!(
        (original.dev(), original.ino()),
        (replacement.dev(), replacement.ino())
    );
    let rejected = request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce);
    assert_ne!(rejected.status, 0);
    assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
    drop(controller);
}

#[test]
fn endpoint_replacement_is_preserved_and_retirement_fails_closed() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    assert_eq!(
        request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce).status,
        0
    );
    let endpoint = fixture.root.join("var/run/shellspawn.sock");
    fs::rename(&endpoint, endpoint.with_extension("original")).unwrap();
    fs::write(&endpoint, b"replacement").unwrap();
    let rejected = request(&controller, CohortEndpoint::Shellspawn, 2, controller.nonce);
    assert_ne!(rejected.status, 0);
    assert_eq!(fs::read(&endpoint).unwrap(), b"replacement");
    drop(controller);
}

#[test]
fn retained_prefix_fd_prevents_path_replacement_redirection() {
    let fixture = Fixture::new();
    let (controller, _darlingserver) =
        CohortController::start_for_test(&fixture.root, 4242).unwrap();
    let retained = fixture.root.with_extension("retained");
    fs::rename(&fixture.root, &retained).unwrap();
    fs::create_dir(&fixture.root).unwrap();
    fs::create_dir_all(fixture.root.join("var/run")).unwrap();
    fs::create_dir_all(fixture.root.join("var/tmp/launchd")).unwrap();

    assert!(!fixture.root.join(".lc-v1.sock").exists());
    assert!(!retained.join("var/run/shellspawn.sock").exists());
    assert!(!fixture.root.join("var/run/shellspawn.sock").exists());
    fs::remove_dir_all(&fixture.root).unwrap();
    fs::rename(retained, &fixture.root).unwrap();
    assert_eq!(
        request(&controller, CohortEndpoint::Shellspawn, 1, controller.nonce).status,
        0
    );
    controller.finish().unwrap();
}

#[test]
fn ffi_null_layout_and_status_contract_is_stable() {
    assert_eq!(ABANDON_PENDING_STATUS, 3);
    assert_eq!(CLEANUP_PENDING_STATUS, 4);
    assert_eq!(RECOVERY_PENDING_STATUS, 5);
    assert_eq!(size_of::<CohortBootstrap>(), 156);
    assert_eq!(std::mem::align_of::<CohortBootstrap>(), 4);

    assert!(unsafe {
        darling_lifecycle_cohort_start(-1, -1, std::ptr::null(), -1, std::ptr::null_mut())
    }
    .is_null());
    assert_eq!(
        unsafe { darling_lifecycle_cohort_finish(std::ptr::null_mut()) },
        -1
    );
    assert_eq!(
        unsafe { darling_lifecycle_cohort_abandon(std::ptr::null_mut()) },
        -1
    );
    assert_eq!(
        unsafe { darling_lifecycle_cohort_worker_pid(std::ptr::null_mut()) },
        -1
    );
    assert!(!unsafe { darling_lifecycle_cohort_admission_open(std::ptr::null_mut()) });
    assert_eq!(
        unsafe { darling_lifecycle_cohort_prepare_var_run(std::ptr::null_mut()) },
        -1
    );
    assert_eq!(
        unsafe {
            darling_lifecycle_cohort_prepare_user_home(std::ptr::null_mut(), std::ptr::null())
        },
        -1
    );
    assert_eq!(
        unsafe {
            darling_lifecycle_cohort_send_guest_namespace_bootstrap(std::ptr::null_mut(), -1)
        },
        -1
    );
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_configure(std::ptr::null_mut()) },
        -1
    );
    assert_eq!(
        unsafe { darling_lifecycle_guest_namespace_directory(std::ptr::null_mut()) },
        -1
    );
    assert_eq!(
        unsafe {
            darling_lifecycle_guest_namespace_transaction(
                std::ptr::null_mut(),
                std::ptr::null(),
                std::ptr::null_mut(),
            )
        },
        -1
    );
}

#[test]
fn internal_authority_duplicates_are_cloexec() {
    let fixture = Fixture::new();
    let prefix = open_prefix(&fixture.root).unwrap();
    let direct = duplicate(prefix.as_fd()).unwrap();
    assert!(rustix::io::fcntl_getfd(&direct)
        .unwrap()
        .contains(rustix::io::FdFlags::CLOEXEC));

    let nested = open_directory_chain(prefix.as_fd(), &[b"var", b"run"]).unwrap();
    assert!(rustix::io::fcntl_getfd(&nested)
        .unwrap()
        .contains(rustix::io::FdFlags::CLOEXEC));

    let authority = SessionAuthority::acquire(&fixture.root, std::process::id() as _).unwrap();
    let deployment_prefix = duplicate(authority.prefix.as_fd()).unwrap();
    assert!(rustix::io::fcntl_getfd(&deployment_prefix)
        .unwrap()
        .contains(rustix::io::FdFlags::CLOEXEC));
}
