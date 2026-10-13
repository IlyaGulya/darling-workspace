/*
 * thread_get_special_reply_port_lane_route_guest.c -- permanent guest oracle for
 * thread_get_special_reply_port.
 *
 * WHY THIS EXISTS. libxpc's synchronous path -- xpc_connection_send_message_with_reply_sync ->
 * dispatch_mach_send_with_result_and_wait_for_reply -> _dispatch_mach_send_and_wait_for_reply ->
 * _dispatch_get_thread_reply_port -- mints the calling thread's special reply port ONCE per thread
 * and caches it in a TSD. In the guest that trap had NO route at all: the generated wrapper asked
 * the RETIRED per-thread RPC socket for a transport, mach_driver_get_fd() declined unconditionally,
 * and the trap's own implementation swallowed the refusal into MACH_PORT_NULL. libdispatch treats a
 * NULL (or DEAD) port as unreachable and traps natively -- `ud2` in
 * _dispatch_get_thread_reply_port + 0x107 (deployed libdispatch.dylib file+0x36977) -- killing the
 * main thread of xcodebuild before it built anything. MEASURED on prefix-verify14, BOTH xcodebuild
 * workloads:
 *
 *   [rpc-socket-DENIED] pid=797268 tid=797268 call=thread_get_special_reply_port image=kernel delta=0x33D2D denied=1
 *   [sigexc-fault-map 7aca9ac00000-7aca9ac84000 r-xp 00001000 ... /usr/lib/system/libdispatch.dylib]
 *   [sigexc-fatal sig=4 code=2 addr=0x7ACA9AC35977 pid=797268 tid=797268]
 *   XCB_RC=132
 *
 * and delta=0x33D2D resolves against the DEPLOYED libsystem_kernel to
 * `dserver_rpc_thread_get_special_reply_port + 0x1d` (the generated wrapper's up-front
 * dserver_rpc_hooks_get_socket()). mach_driver_get_fd does not exist in the server binary, so the
 * denial is the guest's own retired per-thread socket refusing the transport and the server never
 * saw the call.
 *
 * THE MACOS-SHAPED CONTRACT THIS PINS. thread_get_special_reply_port() allocates the calling
 * thread's special reply port and returns a mach_port_name_t carrying a SEND right and a RECEIVE
 * right (duct-tape xnu/osfmk/kern/ipc_tt.c:1319, which is Apple's own routine). It returns
 * MACH_PORT_NULL only on a resource failure, and each call first UNBINDS the thread's previous
 * special reply port -- so a second call is legal and its result is usable in exactly the same way.
 *
 * NEGATIVE ARM (what makes this a regression proof rather than a smoke test): on the unfixed runtime
 * the FIRST call returns MACH_PORT_NULL while the same process's mach_reply_port() -- the twin trap
 * that was already routed over the same lane -- keeps working, which is what separates "this call has
 * no route" from "the runtime is broken". The RED arm therefore reports a NULL/DEAD port and the
 * denial line above, and the GREEN arm reports a live SEND right for BOTH calls.
 *
 * CONTROLS THAT MUST NOT DIE. mach_reply_port() must keep returning a usable port (positive control),
 * and mach_port_type() on a name that no right backs must NOT report a live right and must not kill
 * the process (the "answers rather than kills" arm; this tree answers KERN_SUCCESS/type 0 for it,
 * which is recorded here rather than asserted to be KERN_INVALID_NAME). Reaching the verdict line at
 * all is itself the "did not die" evidence: had any of these killed the caller, no line would follow.
 *
 * The raw measurement lines are printed unconditionally (SPRPROBE ...) so a RED arm can quote
 * port/kr/type from the run's own output, and the verdict line is read by the harness's ok-marker.
 *
 * Deliberately NOT exercised: mach_port_deallocate() on the minted names. That trap is UDS-only in
 * this tree's lane-class table and a failure there would be a claim about a DIFFERENT call; this
 * oracle measures the minting call and nothing else. The process exits after the verdict.
 */

#include <mach/mach.h>
#include <mach/mach_traps.h>
#include <mach/port.h>
#include <stdio.h>
#include <unistd.h>

/* The guest SDK's mach/mach_traps.h carries pid_for_task but not the reply-port traps; the
 * canonical prototypes are xnu/osfmk/mach/mach_traps.h:88 and :90. */
extern mach_port_name_t mach_reply_port(void);
extern mach_port_name_t thread_get_special_reply_port(void);

static int failures;

static void fail(const char *msg)
{
	printf("SPRPROBE-FAIL %s\n", msg);
	failures++;
}

/* A name that passes MACH_PORT_VALID but is not a name of ours: no right backs it. */
#define BOGUS_PORT_NAME 0x7ffffff0u

/*
 * The load-bearing assertion: the returned name is neither MACH_PORT_NULL nor MACH_PORT_DEAD, and it
 * really names a live right in this task -- read back with mach_port_type(), an already-routed call
 * (DSERVER_PROCESS_CONTROL_OP_MACH_PORT_TYPE), so "the port is usable" is measured rather than
 * assumed. Prints the raw values so a RED arm can quote them.
 */
static int check_minted(const char *what, mach_port_name_t name)
{
	mach_port_type_t type = 0;
	kern_return_t kr;

	printf("SPRPROBE %s port=0x%x\n", what, (unsigned)name);
	if (name == MACH_PORT_NULL) {
		fail("a minted port came back MACH_PORT_NULL");
		return 0;
	}
	if (name == MACH_PORT_DEAD) {
		fail("a minted port came back MACH_PORT_DEAD");
		return 0;
	}
	kr = mach_port_type(mach_task_self(), name, &type);
	printf("SPRPROBE %s port_type kr=%d type=0x%x\n", what, (int)kr, (unsigned)type);
	if (kr != KERN_SUCCESS) {
		fail("mach_port_type on a minted port failed");
		return 0;
	}
	if ((type & MACH_PORT_TYPE_SEND) == 0) {
		fail("a minted port is not a live SEND right");
		return 0;
	}
	return 1;
}

int main(void)
{
	int first_ok;
	int second_ok;
	mach_port_name_t reply_port;
	mach_port_type_t type = 0;
	kern_return_t kr;

	/* Unbuffered: a raw measurement line that is still sitting in a stdio buffer when the caller
	 * blocks is not evidence, and "where did it stop" is exactly what a hang here must answer. */
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("SPRPROBE getpid=%d\n", (int)getpid());

	/* --- 1. THE SUBJECT: the call _dispatch_get_thread_reply_port makes exactly once per thread --- */
	printf("SPRPROBE step=first\n");
	first_ok = check_minted("first", thread_get_special_reply_port());

	/* --- 2. a second call: XNU unbinds the previous special reply port first, so this is legal and
	 *        its result must be usable too (this is the path that unbinds+re-mints) --- */
	printf("SPRPROBE step=second\n");
	second_ok = check_minted("second", thread_get_special_reply_port());

	/* --- 3. positive control: the twin trap that was already routed over the same lane must keep
	 *        working, so a failure above cannot be explained as "the runtime is broken" --- */
	printf("SPRPROBE step=reply_port\n");
	reply_port = mach_reply_port();
	printf("SPRPROBE reply_port port=0x%x\n", (unsigned)reply_port);
	if (reply_port == MACH_PORT_NULL || reply_port == MACH_PORT_DEAD) {
		fail("the control call mach_reply_port() stopped returning a usable port");
	}

	/* --- 4. the control that must fail rather than die: an unbacked name is an error, not a kill --- */
	printf("SPRPROBE step=bogus\n");
	kr = mach_port_type(mach_task_self(), (mach_port_name_t)BOGUS_PORT_NAME, &type);
	printf("SPRPROBE bogus_port_type kr=%d type=0x%x\n", (int)kr, (unsigned)type);
	if (kr == KERN_SUCCESS && (type & MACH_PORT_TYPE_SEND) != 0) {
		fail("an unbacked port name reported a live SEND right");
	}

	/* Reaching this line is the "none of the above killed the process" evidence. */
	printf("SPRPROBE survived_controls=1 first_ok=%d second_ok=%d\n", first_ok, second_ok);

	printf("SPRPROBE failures=%d\n", failures);
	if (failures == 0) {
		printf("THREAD_GET_SPECIAL_REPLY_PORT_LANE_ROUTE_OK\n");
		return 0;
	}
	printf("THREAD_GET_SPECIAL_REPLY_PORT_LANE_ROUTE_FAIL\n");
	return 1;
}
