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
unsafe fn exec_controller_worker(worker_fd: RawFd, bootstrap_fd: RawFd) -> ! {
    const WORKER_ARG0: &[u8] = b"darling-lifecycle-controller-worker\0";
    let argv = [WORKER_ARG0.as_ptr().cast::<libc::c_char>(), ptr::null()];
    let envp = [ptr::null::<libc::c_char>()];
    // SAFETY: source descriptors were duplicated above 9 before fork; targets
    // 3 and 4 are child-local and dup3 is async-signal-safe.
    if unsafe { libc::dup3(bootstrap_fd, 3, 0) } < 0 || unsafe { libc::dup3(worker_fd, 4, 0) } < 0 {
        // SAFETY: _exit is async-signal-safe and does not run destructors.
        unsafe { libc::_exit(126) };
    }
    // SAFETY: close_range is an async-signal-safe Linux syscall; descriptors
    // 0..=4 are the only deliberately inherited set.
    if unsafe { libc::syscall(libc::SYS_close_range, 5u32, u32::MAX, 0u32) } < 0 {
        // SAFETY: _exit is async-signal-safe and does not run destructors.
        unsafe { libc::_exit(126) };
    }
    // SAFETY: fd 4 is a retained O_PATH executable, argv/envp are static,
    // NUL-terminated arrays prepared before fork, and execveat is syscall-only.
    unsafe {
        libc::syscall(
            libc::SYS_execveat,
            4,
            c"".as_ptr(),
            argv.as_ptr(),
            envp.as_ptr(),
            libc::AT_EMPTY_PATH,
        )
    };
    // SAFETY: _exit is async-signal-safe and does not run destructors.
    unsafe { libc::_exit(127) };
}

#[cfg(not(test))]
pub(super) fn worker_setsid() -> Result<(), CohortError> {
    // SAFETY: executed after exec in the dedicated single-threaded worker;
    // setsid has no pointer or ownership preconditions.
    if unsafe { libc::setsid() } < 0 {
        Err(io_error("setsid(controller worker)"))
    } else {
        Ok(())
    }
}

#[cfg(not(test))]
pub(super) fn close_worker_trampoline_descriptors() {
    // SAFETY: after exec, fd 3 has already been duplicated into an OwnedFd and
    // fd 4 was used only as the execveat executable. These inherited raw
    // descriptors are closed exactly once before authority is received.
    unsafe {
        rustix::io::close(3);
        rustix::io::close(4);
    }
}

#[cfg(not(test))]
pub(super) fn send_worker_bootstrap(
    socket: RawFd,
    envelope: ControllerWorkerBootstrap,
    descriptors: &[RawFd; CONTROLLER_WORKER_FD_COUNT],
) -> Result<(), CohortError> {
    let socket = crate::inherited_fd::duplicate_cloexec(socket, 5)
        .map_err(|error| CohortError::Io("duplicate(worker bootstrap)", error))?;
    let owned = descriptors
        .iter()
        .map(|fd| crate::inherited_fd::duplicate_cloexec(*fd, 5))
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| CohortError::Io("duplicate(worker authority)", error))?;
    let borrowed = owned.iter().map(OwnedFd::as_fd).collect::<Vec<_>>();
    // SAFETY: the repr(C) envelope contains only integers and a byte array and
    // remains immutable for the duration of sendmsg.
    let bytes = unsafe {
        std::slice::from_raw_parts(
            (&envelope as *const ControllerWorkerBootstrap).cast::<u8>(),
            size_of::<ControllerWorkerBootstrap>(),
        )
    };
    let vectors = [io::IoSlice::new(bytes)];
    let mut space =
        [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(CONTROLLER_WORKER_FD_COUNT))];
    let mut ancillary = rustix::net::SendAncillaryBuffer::new(&mut space);
    if !ancillary.push(rustix::net::SendAncillaryMessage::ScmRights(&borrowed)) {
        return Err(CohortError::Protocol("worker SCM_RIGHTS buffer"));
    }
    let sent = rustix::net::sendmsg(
        &socket,
        &vectors,
        &mut ancillary,
        rustix::net::SendFlags::NOSIGNAL,
    )
    .map_err(|error| CohortError::Io("sendmsg(worker bootstrap)", error.into()))?;
    if sent != bytes.len() {
        return Err(CohortError::Protocol("worker bootstrap size"));
    }
    Ok(())
}

pub(super) fn receive_worker_bootstrap(
    socket: RawFd,
) -> Result<(ControllerWorkerBootstrap, Vec<OwnedFd>), CohortError> {
    let socket = crate::inherited_fd::duplicate_cloexec(socket, 5)
        .map_err(|error| CohortError::Io("duplicate(worker receive)", error))?;
    let mut envelope = MaybeUninit::<ControllerWorkerBootstrap>::zeroed();
    // SAFETY: the destination spans exactly one envelope and is not read until
    // recvmsg reports the exact byte count.
    let buffer = unsafe {
        std::slice::from_raw_parts_mut(
            envelope.as_mut_ptr().cast::<u8>(),
            size_of::<ControllerWorkerBootstrap>(),
        )
    };
    let mut vectors = [io::IoSliceMut::new(buffer)];
    let mut space =
        [MaybeUninit::uninit(); rustix::cmsg_space!(ScmRights(CONTROLLER_WORKER_FD_COUNT + 1))];
    let mut ancillary = rustix::net::RecvAncillaryBuffer::new(&mut space);
    let message = rustix::net::recvmsg(
        &socket,
        &mut vectors,
        &mut ancillary,
        rustix::net::RecvFlags::CMSG_CLOEXEC,
    )
    .map_err(|error| CohortError::Io("recvmsg(worker bootstrap)", error.into()))?;
    if message.bytes != size_of::<ControllerWorkerBootstrap>()
        || message.flags.contains(rustix::net::ReturnFlags::TRUNC)
        || message.flags.contains(rustix::net::ReturnFlags::CTRUNC)
    {
        return Err(CohortError::Protocol("worker bootstrap truncation"));
    }
    let mut descriptors = Vec::new();
    for item in ancillary.drain() {
        match item {
            rustix::net::RecvAncillaryMessage::ScmRights(fds) => descriptors.extend(fds),
            _ => return Err(CohortError::Protocol("worker ancillary type")),
        }
    }
    if descriptors.len() != CONTROLLER_WORKER_FD_COUNT {
        return Err(CohortError::Protocol("worker descriptor count"));
    }
    // SAFETY: every bit pattern is valid and recvmsg filled the exact object.
    Ok((unsafe { envelope.assume_init() }, descriptors))
}

#[cfg(not(test))]
pub(super) fn send_worker_ready(socket: RawFd) -> Result<(), CohortError> {
    let socket = crate::inherited_fd::duplicate_cloexec(socket, 5)
        .map_err(|error| CohortError::Io("duplicate(worker ready)", error))?;
    let sent = rustix::net::send(&socket, &[1], rustix::net::SendFlags::NOSIGNAL)
        .map_err(|error| CohortError::Io("send(worker ready)", error.into()))?;
    if sent == 1 {
        Ok(())
    } else {
        Err(CohortError::Protocol("worker ready size"))
    }
}

#[cfg(not(test))]
pub(super) fn spawn_controller_process(
    mut authority: SessionAuthority,
    listener: OwnedFd,
    child_control: OwnedFd,
    worker_fd: RawFd,
    nonce: [u8; NONCE_BYTES],
) -> Result<ProcessWorker, CohortError> {
    let parent_pid = std::process::id() as libc::pid_t;
    let (bootstrap_parent, bootstrap_child) = rustix::net::socketpair(
        rustix::net::AddressFamily::UNIX,
        rustix::net::SocketType::SEQPACKET,
        rustix::net::SocketFlags::CLOEXEC,
        None,
    )
    .map_err(|error| {
        CohortError::Io(
            "socketpair(controller bootstrap)",
            io::Error::from_raw_os_error(error.raw_os_error()),
        )
    })?;
    let worker_exec = crate::inherited_fd::duplicate_cloexec(worker_fd, 10)
        .map_err(|error| CohortError::Io("duplicate(worker executable)", error))?;
    let child_bootstrap =
        crate::inherited_fd::duplicate_cloexec(bootstrap_child.as_raw_fd(), 10)
            .map_err(|error| CohortError::Io("duplicate(worker child socket)", error))?;
    let descriptors = authority.worker_fds(&listener, &child_control)?;

    // SAFETY: the child branch immediately enters a syscall-only trampoline;
    // it performs no allocation, formatting, collection access, or destructor.
    let child = unsafe { libc::fork() };
    if child < 0 {
        return Err(io_error("fork(controller)"));
    }
    if child == 0 {
        // SAFETY: all arguments were prepared before fork. This function never
        // returns and contains only dup3, close_range, execveat, and _exit.
        unsafe { exec_controller_worker(worker_exec.as_raw_fd(), child_bootstrap.as_raw_fd()) }
    }

    drop(bootstrap_child);
    drop(child_bootstrap);
    drop(worker_exec);
    let child_pidfd = match pidfd_open(child) {
        Ok(pidfd) => pidfd,
        Err(error) => {
            // No numeric-PID signal fallback is permitted. Closing the only
            // parent bootstrap endpoint makes the worker's bounded receive
            // fail; reap that exact child identity before returning.
            drop(bootstrap_parent);
            if let Some(pid) = rustix::process::Pid::from_raw(child) {
                loop {
                    match rustix::process::waitpid(Some(pid), rustix::process::WaitOptions::empty())
                    {
                        Ok(Some(_)) | Err(rustix::io::Errno::CHILD) => break,
                        Ok(None) | Err(rustix::io::Errno::INTR) => continue,
                        Err(_) => break,
                    }
                }
            }
            return Err(error);
        }
    };
    let process = ProcessWorker {
        pid: child,
        pidfd: child_pidfd,
    };
    let envelope = ControllerWorkerBootstrap {
        version: CONTROLLER_WORKER_PROTOCOL,
        descriptor_count: CONTROLLER_WORKER_FD_COUNT as u32,
        parent_pid,
        session_root_pid: authority.session_root_pid,
        nonce,
    };
    if let Err(error) = send_worker_bootstrap(bootstrap_parent.as_raw_fd(), envelope, &descriptors)
    {
        terminate_worker(process);
        return Err(error);
    }
    let process = await_worker_ready(process, &bootstrap_parent)?;
    authority.disarm_drop_cleanup();
    drop(authority);
    drop(listener);
    drop(child_control);
    Ok(process)
}

fn terminate_worker(process: ProcessWorker) {
    let _ = rustix::process::pidfd_send_signal(&process.pidfd, rustix::process::Signal::KILL);
    let _ = wait_worker(process);
}

pub(super) fn await_worker_ready(
    process: ProcessWorker,
    bootstrap_parent: &OwnedFd,
) -> Result<ProcessWorker, CohortError> {
    if let Err(error) = wait_for_io(bootstrap_parent.as_raw_fd(), libc::POLLIN) {
        terminate_worker(process);
        return Err(error);
    }
    let mut ready_status = [0u8; 1];
    let received = match rustix::net::recv(
        bootstrap_parent,
        &mut ready_status,
        rustix::net::RecvFlags::empty(),
    ) {
        Ok((count, _)) => count,
        Err(error) => {
            terminate_worker(process);
            return Err(CohortError::Io(
                "recv(controller ready)",
                io::Error::from_raw_os_error(error.raw_os_error()),
            ));
        }
    };
    if received != 1 || ready_status[0] != 1 {
        terminate_worker(process);
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
