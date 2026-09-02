//! Narrow C ABI boundary for lifecycle cohort ownership and wire parsing.

#![deny(clippy::undocumented_unsafe_blocks)]

use super::*;

fn restore_pending_owner(
    owned: Box<CohortController>,
    expected: *mut CohortController,
) -> *mut CohortController {
    let restored = Box::into_raw(owned);
    debug_assert_eq!(restored, expected);
    restored
}

pub(super) fn peer_pidfd_from_socket(socket: RawFd) -> Result<OwnedFd, CohortError> {
    let mut pidfd: c_int = -1;
    let mut length = size_of::<c_int>() as libc::socklen_t;
    // SAFETY: both output pointers refer to initialized, correctly sized
    // storage; the kernel creates a new descriptor only on success.
    let result = unsafe {
        libc::getsockopt(
            socket,
            libc::SOL_SOCKET,
            SO_PEERPIDFD,
            (&mut pidfd as *mut c_int).cast(),
            &mut length,
        )
    };
    if result < 0 || length as usize != size_of::<c_int>() || pidfd < 0 {
        if pidfd >= 0 {
            // SAFETY: a nonnegative descriptor returned in the output slot is
            // owned by this failed conversion path.
            unsafe { libc::close(pidfd) };
        }
        return Err(io_error("getsockopt(SO_PEERPIDFD)"));
    }
    // SAFETY: successful SO_PEERPIDFD transfers ownership of this fresh fd.
    let owned = unsafe { OwnedFd::from_raw_fd(pidfd) };
    rustix::io::fcntl_setfd(&owned, rustix::io::FdFlags::CLOEXEC).map_err(|error| {
        CohortError::Io(
            "fcntl(peer pidfd)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    Ok(owned)
}

pub(super) fn probe_pidfd(pidfd: RawFd) -> io::Result<()> {
    // SAFETY: signal 0 performs an identity/liveness probe only; the pidfd is
    // borrowed for the duration of the syscall and no pointer is dereferenced.
    let result = unsafe {
        libc::syscall(
            libc::SYS_pidfd_send_signal,
            pidfd,
            0,
            ptr::null::<libc::siginfo_t>(),
            0,
        )
    };
    if result == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

#[cfg(not(test))]
pub(super) fn close_unowned_descriptor(fd: RawFd) {
    // SAFETY: the caller obtained this fd from the current `/proc/self/fd`
    // census, excluded every retained/stdio/directory fd, and transfers the
    // remaining descriptor to this close operation exactly once.
    unsafe { rustix::io::close(fd) };
}

#[cfg(not(test))]
pub(super) fn spawn_controller_process(
    mut authority: SessionAuthority,
    listener: OwnedFd,
    child_control: OwnedFd,
    shutdown_fd: RawFd,
    darlingserver_fd: RawFd,
    nonce: [u8; NONCE_BYTES],
) -> Result<ProcessWorker, CohortError> {
    let parent_pid = std::process::id() as libc::pid_t;
    let parent_watch = pidfd_open(parent_pid)?;
    let (ready_parent, ready_child) = rustix::net::socketpair(
        rustix::net::AddressFamily::UNIX,
        rustix::net::SocketType::SEQPACKET,
        rustix::net::SocketFlags::CLOEXEC,
        None,
    )
    .map_err(|error| {
        CohortError::Io(
            "socketpair(controller ready)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;

    // SAFETY: this is the single production fork boundary. The child invokes
    // only async-signal-safe syscalls plus the pre-existing controller loop;
    // it never returns into the parent's Rust stack and terminates via _exit.
    let child = unsafe { libc::fork() };
    if child < 0 {
        return Err(io_error("fork(controller)"));
    }
    if child == 0 {
        drop(ready_parent);
        // SAFETY: after fork these are child-local copies of parent-owned
        // descriptors and are closed exactly once before entering the loop.
        unsafe {
            libc::close(shutdown_fd);
            libc::close(darlingserver_fd);
        }
        // SAFETY: setsid has no memory-safety preconditions.
        if unsafe { libc::setsid() } < 0 {
            let ready_status: i32 = -1;
            let _ = write_all(ready_child.as_fd(), &ready_status.to_ne_bytes());
            authority.disarm_drop_cleanup();
            // SAFETY: the fork child must not run Rust destructors on failure.
            unsafe { libc::_exit(1) };
        }
        let mut allowed = authority.retained_fds();
        allowed.extend([
            listener.as_raw_fd(),
            child_control.as_raw_fd(),
            parent_watch.as_raw_fd(),
            ready_child.as_raw_fd(),
        ]);
        let prepared = close_unowned_child_fds(&allowed);
        let ready_status: i32 = if prepared.is_ok() { 0 } else { -1 };
        let _ = write_all(ready_child.as_fd(), &ready_status.to_ne_bytes());
        drop(ready_child);
        if prepared.is_err() {
            authority.disarm_drop_cleanup();
            // SAFETY: see the fork-child termination contract above.
            unsafe { libc::_exit(1) };
        }
        let (mut authority, loop_result, forensic_preserve) = server_loop(
            authority,
            listener,
            child_control,
            Some(parent_watch),
            nonce,
        );
        let cleanup_result = if forensic_preserve {
            authority.disarm_drop_cleanup();
            Ok(())
        } else {
            authority.cleanup_all()
        };
        let status = i32::from(loop_result.is_err() || cleanup_result.is_err());
        // SAFETY: final child termination; no parent-owned Rust stack resumes.
        unsafe { libc::_exit(status) };
    }

    drop(ready_child);
    drop(child_control);
    authority.disarm_drop_cleanup();
    drop(authority);
    drop(listener);
    drop(parent_watch);
    let child_pidfd = match pidfd_open(child) {
        Ok(pidfd) => pidfd,
        Err(error) => {
            if let Some(pid) = rustix::process::Pid::from_raw(child) {
                let _ = rustix::process::kill_process(pid, rustix::process::Signal::KILL);
                let _ = rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::empty());
            }
            return Err(error);
        }
    };
    let process = ProcessWorker {
        pid: child,
        pidfd: child_pidfd,
    };
    if let Err(error) = wait_for_io(ready_parent.as_raw_fd(), libc::POLLIN) {
        if let Some(pid) = rustix::process::Pid::from_raw(child) {
            let _ = rustix::process::kill_process(pid, rustix::process::Signal::KILL);
        }
        let _ = wait_worker(process);
        return Err(error);
    }
    let mut ready_status = [0u8; size_of::<i32>()];
    let received = rustix::net::recv(
        &ready_parent,
        &mut ready_status,
        rustix::net::RecvFlags::empty(),
    )
    .map_err(|error| {
        CohortError::Io(
            "recv(controller ready)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?
    .0;
    if received != size_of::<i32>() || i32::from_ne_bytes(ready_status) != 0 {
        if let Some(pid) = rustix::process::Pid::from_raw(child) {
            let _ = rustix::process::kill_process(pid, rustix::process::Signal::KILL);
        }
        let _ = wait_worker(process);
        return Err(CohortError::Process);
    }
    Ok(process)
}

pub(super) fn receive_wire_request(fd: RawFd) -> Result<WireRequest, CohortError> {
    let mut request = MaybeUninit::<WireRequest>::zeroed();
    // SAFETY: the destination is writable for exactly one WireRequest and the
    // value is assumed initialized only after an exact-size receive.
    let count = unsafe { libc::recv(fd, request.as_mut_ptr().cast(), size_of::<WireRequest>(), 0) };
    if count != size_of::<WireRequest>() as isize {
        return Err(CohortError::Protocol("request size"));
    }
    // SAFETY: every WireRequest field permits all bit patterns and recv filled
    // the complete object.
    Ok(unsafe { request.assume_init() })
}

pub(super) fn send_wire_response(
    fd: RawFd,
    response: WireResponse,
    passed_fd: Option<RawFd>,
) -> Result<(), CohortError> {
    let socket = crate::inherited_fd::duplicate_cloexec(fd, 3)
        .map_err(|error| CohortError::Io("duplicate(response socket)", error))?;
    // SAFETY: WireResponse is repr(C), contains only integer and byte-array
    // fields, and remains live and immutable throughout the send.
    let bytes = unsafe {
        std::slice::from_raw_parts(
            (&response as *const WireResponse).cast::<u8>(),
            size_of::<WireResponse>(),
        )
    };
    let vectors = [io::IoSlice::new(bytes)];
    let flags = rustix::net::SendFlags::NOSIGNAL | rustix::net::SendFlags::DONTWAIT;
    let sent = if let Some(passed_fd) = passed_fd {
        let passed = crate::inherited_fd::duplicate_cloexec(passed_fd, 3)
            .map_err(|error| CohortError::Io("duplicate(SCM_RIGHTS)", error))?;
        let descriptors = [passed.as_fd()];
        let mut space = [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(1))];
        let mut ancillary = rustix::net::SendAncillaryBuffer::new(&mut space);
        if !ancillary.push(rustix::net::SendAncillaryMessage::ScmRights(&descriptors)) {
            return Err(CohortError::Protocol("SCM_RIGHTS buffer"));
        }
        rustix::net::sendmsg(&socket, &vectors, &mut ancillary, flags)
    } else {
        let mut ancillary = rustix::net::SendAncillaryBuffer::default();
        rustix::net::sendmsg(&socket, &vectors, &mut ancillary, flags)
    }
    .map_err(|error| {
        CohortError::Io(
            "sendmsg(response)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    if sent != size_of::<WireResponse>() {
        return Err(CohortError::Protocol("response size"));
    }
    Ok(())
}

pub(super) fn socket_peer_credentials(fd: RawFd) -> Result<libc::ucred, CohortError> {
    let socket = crate::inherited_fd::duplicate_cloexec(fd, 3)
        .map_err(|error| CohortError::Io("duplicate(peer credential socket)", error))?;
    let credentials = rustix::net::sockopt::socket_peercred(&socket).map_err(|error| {
        CohortError::Io(
            "getsockopt(SO_PEERCRED)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    Ok(libc::ucred {
        pid: credentials.pid.as_raw_pid(),
        uid: credentials.uid.as_raw(),
        gid: credentials.gid.as_raw(),
    })
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live controller pointer and `plan` must be
/// null or point to a complete plan for the duration of the call.
pub unsafe extern "C" fn darling_lifecycle_cohort_prepare_user_home(
    controller: *mut CohortController,
    plan: *const crate::preinit_user_home::DarlingLifecycleUserHomePlan,
) -> c_int {
    // SAFETY: both pointers are inspected only for this call. Null is a typed
    // input failure; the C caller owns their storage for the call duration.
    let (Some(controller), Some(plan)) = (unsafe { (controller.as_mut(), plan.as_ref()) }) else {
        return -1;
    };
    // SAFETY: parse_ffi_plan bounds every C string field before reading it;
    // `plan` was validated non-null above and remains borrowed for this call.
    let parsed = unsafe { crate::preinit_user_home::parse_ffi_plan(plan) }
        .map_err(|_| CohortError::Protocol("invalid persistent guest home plan"));
    match parsed.and_then(|plan| controller.prepare_user_home(plan)) {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("persistent guest home preparation refused: {error}");
            -1
        }
    }
}

#[no_mangle]
/// # Safety
/// `prefix_argument` must be a valid NUL-terminated string and `output` valid
/// writable storage; inherited descriptors must remain open for the call.
pub unsafe extern "C" fn darling_lifecycle_cohort_start(
    prefix_fd: c_int,
    deployment_prefix_fd: c_int,
    prefix_argument: *const c_char,
    init_pid: libc::pid_t,
    output: *mut CohortBootstrap,
) -> *mut CohortController {
    if prefix_fd < 0 || deployment_prefix_fd < 0 || prefix_argument.is_null() || output.is_null() {
        return ptr::null_mut();
    }
    // SAFETY: null was rejected and the C contract supplies a NUL-terminated
    // prefix argument that remains live for the duration of this call.
    let prefix_argument = unsafe { CStr::from_ptr(prefix_argument) };
    let (mut controller, darlingserver) = match CohortController::start_from_fd(
        prefix_fd,
        deployment_prefix_fd,
        prefix_argument.to_bytes(),
        init_pid,
    ) {
        Ok(value) => value,
        Err(error) => {
            eprintln!("lifecycle cohort acquisition refused: {error}");
            return ptr::null_mut();
        }
    };
    let dserver_log = match controller.take_dserver_log() {
        Ok(log) => log,
        Err(error) => {
            eprintln!("lifecycle cohort log transfer refused: {error}");
            return ptr::null_mut();
        }
    };
    if controller.control_name.len() > CONTROL_NAME_CAPACITY {
        return ptr::null_mut();
    }
    let mut bootstrap = CohortBootstrap {
        darlingserver_fd: darlingserver.into_raw_fd(),
        dserver_log_fd: dserver_log.into_raw_fd(),
        control_name_len: controller.control_name.len() as u16,
        reserved: 0,
        control_name: [0; CONTROL_NAME_CAPACITY],
        nonce_hex: controller.nonce_hex(),
    };
    bootstrap.control_name[..controller.control_name.len()]
        .copy_from_slice(&controller.control_name);
    // SAFETY: output is non-null and the C contract supplies aligned writable
    // CohortBootstrap storage. This initializes it exactly once.
    unsafe { ptr::write(output, bootstrap) };
    Box::into_raw(Box::new(controller))
}

#[no_mangle]
/// # Safety
/// `controller` must be null or the exact pointer returned by start or a
/// pending finish. Terminal outcomes consume it; pending outcomes retain it.
pub unsafe extern "C" fn darling_lifecycle_cohort_finish(
    controller: *mut CohortController,
) -> c_int {
    if controller.is_null() {
        return -1;
    }
    // SAFETY: the C ownership contract transfers the unique pointer returned
    // by start. Every pending branch below restores this exact allocation.
    let mut owned = unsafe { Box::from_raw(controller) };
    if owned.cleanup_phase == CleanupPhase::CleanupCommitted {
        return match (*owned).finish_after_cleanup_commit() {
            Ok(()) => 0,
            Err(error) => {
                eprintln!("lifecycle cleanup completion failed: {error}");
                -1
            }
        };
    }
    if owned.cleanup_phase == CleanupPhase::Abandoning {
        let _restored = restore_pending_owner(owned, controller);
        return 1;
    }
    if owned.cleanup_phase == CleanupPhase::RecoveryPending {
        let _restored = restore_pending_owner(owned, controller);
        return RECOVERY_PENDING_STATUS;
    }
    owned.cleanup_phase = CleanupPhase::Draining;
    let Some(guest_namespace) = owned.guest_namespace.as_mut() else {
        return -1;
    };
    if guest_namespace.revoke().is_err() {
        let _restored = restore_pending_owner(owned, controller);
        return 1;
    }
    owned.revoke_guest_transactions();
    if owned.guest_transaction_recovery_pending() || owned.preinit_var_run_recovery_pending() {
        owned.cleanup_phase = CleanupPhase::RecoveryPending;
        let _restored = restore_pending_owner(owned, controller);
        return RECOVERY_PENDING_STATUS;
    }
    if owned.request_cleanup().is_err() {
        let status = if owned.cleanup_phase == CleanupPhase::CleanupCommitted {
            CLEANUP_PENDING_STATUS
        } else {
            1
        };
        let _restored = restore_pending_owner(owned, controller);
        return status;
    }
    match (*owned).finish_after_cleanup_commit() {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("lifecycle cleanup completion failed: {error}");
            -1
        }
    }
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live, unconsumed controller pointer.
pub unsafe extern "C" fn darling_lifecycle_cohort_worker_pid(
    controller: *mut CohortController,
) -> libc::pid_t {
    // SAFETY: the pointer is borrowed only for this observation; null is a
    // normal input failure and the owner retains the allocation.
    let Some(controller) = (unsafe { controller.as_mut() }) else {
        return -1;
    };
    #[cfg(not(test))]
    {
        controller.process.as_ref().map_or(-1, |worker| worker.pid)
    }
    #[cfg(test)]
    {
        let _ = controller;
        -1
    }
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live, unconsumed controller pointer.
pub unsafe extern "C" fn darling_lifecycle_cohort_admission_open(
    controller: *mut CohortController,
) -> bool {
    // SAFETY: this is a shared observation of the live allocation. Null is a
    // normal false result and no ownership transition occurs.
    let Some(controller) = (unsafe { controller.as_ref() }) else {
        return false;
    };
    controller.cleanup_phase == CleanupPhase::Active
        && controller
            .guest_namespace
            .as_ref()
            .is_some_and(GuestNamespaceAuthority::is_active)
}

#[no_mangle]
/// # Safety
/// `controller` must be null or the exact live pointer owned by the caller.
/// Terminal outcomes consume it; ABANDON_PENDING retains the same pointer.
pub unsafe extern "C" fn darling_lifecycle_cohort_abandon(
    controller: *mut CohortController,
) -> c_int {
    if controller.is_null() {
        return -1;
    }
    // SAFETY: the C ownership contract transfers the unique pending pointer.
    // ABANDON_PENDING branches restore the exact allocation with into_raw.
    let mut owned = unsafe { Box::from_raw(controller) };
    if matches!(
        owned.cleanup_phase,
        CleanupPhase::CleanupRequested
            | CleanupPhase::CleanupCommitted
            | CleanupPhase::RecoveryPending
            | CleanupPhase::Finished
    ) {
        let _restored = restore_pending_owner(owned, controller);
        return ABANDON_PENDING_STATUS;
    }
    let Some(guest_namespace) = owned.guest_namespace.as_mut() else {
        return -1;
    };
    let _ = guest_namespace.revoke();
    match owned.abandon_after_revocation() {
        Ok(()) => 0,
        Err(_) => {
            let _restored = restore_pending_owner(owned, controller);
            ABANDON_PENDING_STATUS
        }
    }
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live controller pointer; `socket_fd` must
/// identify the authenticated bootstrap transport for the duration of call.
pub unsafe extern "C" fn darling_lifecycle_cohort_send_guest_namespace_bootstrap(
    controller: *mut CohortController,
    socket_fd: c_int,
) -> c_int {
    // SAFETY: controller is borrowed without ownership transfer for this call;
    // null is a normal input failure.
    let Some(controller) = (unsafe { controller.as_ref() }) else {
        return -1;
    };
    if socket_fd < 0
        || controller
            .send_guest_namespace_bootstrap(socket_fd)
            .is_err()
    {
        -1
    } else {
        0
    }
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live, unconsumed controller pointer.
pub unsafe extern "C" fn darling_lifecycle_cohort_prepare_var_run(
    controller: *mut CohortController,
) -> c_int {
    // SAFETY: the live controller is exclusively borrowed for this call; null
    // is a normal input failure.
    let Some(controller) = (unsafe { controller.as_mut() }) else {
        return -1;
    };
    match controller.prepare_var_run() {
        Ok(_) => 0,
        Err(error) => {
            eprintln!("generation var/run preparation refused: {error}");
            -1
        }
    }
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live, unconsumed controller pointer.
pub unsafe extern "C" fn darling_lifecycle_guest_namespace_configure(
    controller: *mut CohortController,
) -> c_int {
    // SAFETY: shared borrow for this call only; null is a normal input failure.
    let Some(controller) = (unsafe { controller.as_ref() }) else {
        return -1;
    };
    match controller.configure_guest_transactions() {
        Ok(()) => 0,
        Err(error) => {
            eprintln!("guest transaction configuration refused: {error}");
            -1
        }
    }
}

#[no_mangle]
/// # Safety
/// `controller` must be null or a live, unconsumed controller pointer. The
/// returned descriptor is a new caller-owned duplicate on success.
pub unsafe extern "C" fn darling_lifecycle_guest_namespace_directory(
    controller: *mut CohortController,
) -> c_int {
    // SAFETY: shared borrow for this call only; null is a normal input failure.
    let Some(controller) = (unsafe { controller.as_ref() }) else {
        return -1;
    };
    controller
        .duplicate_vchroot_directory()
        .map(OwnedFd::into_raw_fd)
        .unwrap_or(-1)
}

#[no_mangle]
/// # Safety
/// All pointers must be null or valid for their documented read/write access
/// for the duration of the call; `controller` must remain live and unconsumed.
pub unsafe extern "C" fn darling_lifecycle_guest_namespace_transaction(
    controller: *mut CohortController,
    request: *const GuestTransactionWireRequest,
    result: *mut GuestTransactionWireResult,
) -> c_int {
    // SAFETY: all pointers are borrowed only for this call. Null is rejected;
    // request arrays are copied before semantic validation and result is the
    // caller-provided aligned result object.
    let (Some(controller), Some(request), Some(result)) =
        (unsafe { (controller.as_ref(), request.as_ref(), result.as_mut()) })
    else {
        return -1;
    };
    let source_len = usize::from(request.source_length);
    let destination_len = usize::from(request.destination_length);
    if source_len == 0
        || source_len > GUEST_TRANSACTION_PATH_CAPACITY
        || destination_len > GUEST_TRANSACTION_PATH_CAPACITY
    {
        return -1;
    }
    let Ok(id) = TransactionId::new(request.transaction_id) else {
        return -1;
    };
    let source = request.source[..source_len].to_vec();
    let wants_created_fd = request.operation == 1;
    let request = match request.operation {
        1 if destination_len == 0 => GuestTransactionRequest::Create {
            id,
            path: source,
            flags: request.flags,
            mode: request.mode,
        },
        2 if destination_len == 0 => GuestTransactionRequest::Mkdir {
            id,
            path: source,
            mode: request.mode,
        },
        3 if destination_len == 0 => GuestTransactionRequest::Unlink {
            id,
            path: source,
            flags: request.flags,
        },
        4 if destination_len > 0 => GuestTransactionRequest::Rename {
            id,
            source,
            destination: request.destination[..destination_len].to_vec(),
        },
        _ => return -1,
    };
    let Ok(outcome) = controller.execute_guest_transaction(request) else {
        return -1;
    };
    *result = match outcome {
        GuestTransactionOutcome::Created { device, inode } => {
            let created_fd = if wants_created_fd {
                let Ok(slot) = controller.guest_transactions.lock() else {
                    return -1;
                };
                let Some(service) = slot.as_ref() else {
                    return -1;
                };
                let Ok(fd) = service.duplicate_created_result(id, device, inode) else {
                    return -1;
                };
                fd.into_raw_fd()
            } else {
                -1
            };
            GuestTransactionWireResult {
                result: 0,
                disposition: 1,
                device,
                inode,
                created_fd,
                reserved: 0,
            }
        }
        GuestTransactionOutcome::Mutated => GuestTransactionWireResult {
            result: 0,
            disposition: 2,
            device: 0,
            inode: 0,
            created_fd: -1,
            reserved: 0,
        },
        GuestTransactionOutcome::Rejected { errno, .. } => GuestTransactionWireResult {
            result: -errno,
            disposition: 3,
            device: 0,
            inode: 0,
            created_fd: -1,
            reserved: 0,
        },
        GuestTransactionOutcome::RecoveryRequired { .. } => GuestTransactionWireResult {
            result: -libc::EIO,
            disposition: 4,
            device: 0,
            inode: 0,
            created_fd: -1,
            reserved: 0,
        },
    };
    0
}
