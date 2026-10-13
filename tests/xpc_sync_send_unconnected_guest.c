/*
 * xpc_sync_send_unconnected_guest.c -- permanent guest oracle for a SYNCHRONOUS XPC send on a
 * connection whose libdispatch channel was never connected.
 *
 * WHY THIS EXISTS. libxpc's -sendMessageWithSynchronousReply: (src/connection.m) went straight to
 * dispatch_mach_send_with_result_and_wait_for_reply without ever ensuring the connection was
 * activated. libdispatch only clears DISPATCH_MACH_NEVER_CONNECTED from dmsr_disconnect_cnt inside
 * dispatch_mach_connect (libdispatch/src/mach.c:286), which Darling reaches ONLY from
 * activateLocked (connection.m:803) <- activate <- resume. While that bit is still set,
 * _dispatch_mach_send_push_and_trydrain (mach.c:1623-1634) queues the message and returns WITHOUT
 * draining: no request is sent, no reply waiter is ever registered, and the receive that follows is
 * unbounded -- it is issued with MACH_MSG_TIMEOUT_NONE and, because dmsr_send is MACH_PORT_NULL,
 * without MACH_RCV_SYNC_WAIT (mach.c:801-803, gated on MACH_PORT_VALID(send)). Nothing can wake it.
 * That is an infinite park, not a failed send.
 *
 * MEASURED 2026-10-13 on prefix-verify14 (bead dar-liaq, the xcodebuild ladder's wall). xcodebuild's
 * main thread sat in exactly that receive for the whole 40-minute run:
 *
 *   dtape.ering seq=24814 tag=recv_park  a=... b=... c=0xb03 d=0x700020e00000000
 *     option=0x0700020E = MACH_RCV_MSG|MACH_RCV_LARGE|MACH_RCV_LARGE_IDENTITY|MACH_MSG_STRICT_REPLY|
 *                         MACH_RCV_TRAILER_ELEMENTS(7), timeout=0=MACH_MSG_TIMEOUT_NONE,
 *                         MACH_RCV_SYNC_WAIT absent
 *
 * with NO request published on the ring for it (the thread's ring ops are
 * thread_get_special_reply_port then the receive), reached from
 * xpc_connection_send_message_with_reply_sync <- CoreSymbolicationDT <- DVTStackBacktrace <-
 * IDEInitialize. The caller is not the divergence: CoreSymbolicationDT's CSCppDaemonConnection
 * constructor creates the connection for com.apple.coresymbolicationd and calls
 * xpc_connection_resume (Xcode 26.3 CoreSymbolicationDT.framework: create_mach_service at 0xfcac5/
 * 0xfcad4, resume at 0xfcaf9, sync send at 0xfccd4/0xfcce0), so activation WAS attempted and its
 * FAILURE was silently converted into an infinite park instead of a failed send. Apple connects a
 * mach-service connection at creation, so the same peer-that-does-not-exist fails the send there.
 *
 * THE CONTRACT THIS PINS. A synchronous send RETURNS on such a connection -- with an error object
 * (XPC_ERROR_CONNECTION_INTERRUPTED for a named service, XPC_ERROR_CONNECTION_INVALID otherwise) --
 * and the process survives to print its verdict. It does NOT park. The fix therefore does not add a
 * timeout to the receive (that would make a correctness bug flaky): it refuses to enter the
 * unbounded receive, and activates first so that the connected case keeps its bounded receive.
 *
 * TWO SUBJECTS, because both spellings of "never connected" reach the same receive:
 *   A. a connection that is NEVER RESUMED -- the pre-connect sync send the architecture review
 *      names (Apple's own libdispatch marks this phase incomplete at mach.c:1952,
 *      rdar://31823384). The service name is unique to this run, so no receiver can exist for it
 *      under any deployment.
 *   B. the workload's own case: the connection IS resumed, and activation FAILS because no receiver
 *      is registered for com.apple.coresymbolicationd. The bootstrap lookup result is printed, so the
 *      missing receiver is measured here rather than assumed.
 *
 * NEGATIVE ARM (what makes this a regression proof rather than a smoke test): on the unfixed runtime
 * subject A never returns -- the runner's guest timeout kills the shell and the last line is
 * `XPCUNCONN step=send_sync_never_resumed_begin`, with no `..._returned` line and no verdict. The
 * GREEN arm prints `returned=1 type=error` for BOTH subjects.
 *
 * CONTROLS THAT MUST NOT DIE. (1) The bootstrap lookup for the workload's service is printed raw, so
 * "the peer does not exist" is evidence and not an inference. (2) A plain mach_msg send/receive round
 * trip on a port pair this process mints itself must still complete, which separates "the sync-send
 * path parks" from "the runtime's message path is broken". (3) Reaching the verdict line is itself the
 * "none of the above killed the process" evidence.
 *
 * Deliberately NOT exercised: the async send path (-sendMessage:queue:withReply:) and cancel, which
 * are separate contracts; this oracle measures the synchronous send and nothing else.
 */

#include <mach/mach.h>
#include <servers/bootstrap.h>
#include <xpc/xpc.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

/* The service Xcode's CoreSymbolicationDT synchronously sends to, from the literal in the deployed
 * Xcode 26.3 CoreSymbolicationDT.framework (Contents/SharedFrameworks/CoreSymbolicationDT.framework/
 * Versions/A/CoreSymbolicationDT, literal pool for "com.apple.coresymbolicationd"). */
#define WORKLOAD_SERVICE "com.apple.coresymbolicationd"

static int failures;

static void fail(const char *msg)
{
	printf("XPCUNCONN-FAIL %s\n", msg);
	failures++;
}

static const char *type_name(xpc_object_t object)
{
	xpc_type_t type = xpc_get_type(object);

	if (type == XPC_TYPE_ERROR) {
		return "error";
	}
	if (type == XPC_TYPE_DICTIONARY) {
		return "dictionary";
	}
	if (type == XPC_TYPE_CONNECTION) {
		return "connection";
	}
	return "other";
}

/* The load-bearing assertion: the synchronous send came back at all, and what it came back with is an
 * object rather than a NULL. Prints the raw type so a RED/GREEN arm can quote it. */
static int check_returned(const char *what, xpc_object_t reply)
{
	const char *description = NULL;

	if (reply == NULL) {
		fail("the synchronous send returned NULL instead of an object");
		return 0;
	}
	printf("XPCUNCONN %s returned=1 type=%s\n", what, type_name(reply));
	if (xpc_get_type(reply) == XPC_TYPE_ERROR) {
		description = xpc_dictionary_get_string(reply, XPC_ERROR_KEY_DESCRIPTION);
		printf("XPCUNCONN %s error=%s\n", what, description ? description : "(no description)");
	}
	return 1;
}

static xpc_object_t make_message(void)
{
	xpc_object_t message = xpc_dictionary_create(NULL, NULL, 0);

	if (message == NULL) {
		fail("could not create the request dictionary");
		return NULL;
	}
	xpc_dictionary_set_string(message, "XPCUNCONN", "1");
	return message;
}

/* Control: the guest's mach message path itself still works, on ports this process owns. A park in the
 * sync-send arm above must not be explainable as "mach_msg is broken here". The receive buffer is
 * deliberately larger than the message: a MACH_RCV_MSG receive also writes a trailer, and a buffer that
 * fits only the message reports MACH_RCV_TOO_LARGE (0x10004004) instead of delivering it. */
static void control_mach_round_trip(void)
{
	mach_port_t receive_port = MACH_PORT_NULL;
	mach_port_t send_port = MACH_PORT_NULL;
	mach_msg_size_t message_size;
	union {
		struct {
			mach_msg_header_t header;
			char payload[16];
		} sent;
		char receive_buffer[128];
	} message;
	kern_return_t kr;

	kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &receive_port);
	if (kr != KERN_SUCCESS) {
		fail("mach_port_allocate(receive) failed");
		return;
	}
	kr = mach_port_insert_right(mach_task_self(), receive_port, receive_port, MACH_MSG_TYPE_MAKE_SEND);
	if (kr != KERN_SUCCESS) {
		fail("mach_port_insert_right(MAKE_SEND) failed");
		return;
	}
	send_port = receive_port;

	message_size = (mach_msg_size_t)sizeof(message.sent);
	memset(&message, 0, sizeof(message));
	message.sent.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
	message.sent.header.msgh_size = message_size;
	message.sent.header.msgh_remote_port = send_port;
	message.sent.header.msgh_local_port = MACH_PORT_NULL;
	message.sent.header.msgh_id = 0x58504355; /* 'XPCU' */
	memcpy(message.sent.payload, "round-trip", sizeof("round-trip"));

	kr = mach_msg(&message.sent.header, MACH_SEND_MSG, message_size, 0, MACH_PORT_NULL, 0, 0);
	printf("XPCUNCONN mach_send kr=%d\n", (int)kr);
	if (kr != KERN_SUCCESS) {
		fail("a plain mach_msg send on an owned port pair failed");
		return;
	}

	memset(&message, 0, sizeof(message));
	kr = mach_msg((mach_msg_header_t *)&message.receive_buffer, MACH_RCV_MSG, 0,
			(mach_msg_size_t)sizeof(message.receive_buffer), receive_port, 0, 0);
	printf("XPCUNCONN mach_recv kr=%d id=0x%x payload=%s\n", (int)kr,
			(unsigned)message.sent.header.msgh_id, message.sent.payload);
	if (kr != KERN_SUCCESS || message.sent.header.msgh_id != 0x58504355) {
		fail("a plain mach_msg receive on an owned port pair did not deliver the message");
	}
}

int main(void)
{
	char unique_service[128];
	char checked_in_service[128];
	mach_port_t bootstrap = MACH_PORT_NULL;
	mach_port_t service_port = MACH_PORT_NULL;
	kern_return_t kr;
	kern_return_t checkin_kr = (kern_return_t)-1;
	xpc_connection_t connection = NULL;
	xpc_object_t message = NULL;
	xpc_object_t reply = NULL;

	/* Unbuffered: a raw line still sitting in a stdio buffer when the caller blocks is not evidence,
	 * and "where did it stop" is exactly what a hang here must answer. */
	setvbuf(stdout, NULL, _IONBF, 0);
	printf("XPCUNCONN getpid=%d uid=%d euid=%d\n", (int)getpid(), (int)getuid(), (int)geteuid());

	/* --- control 1: the workload's service has no receiver in this guest's bootstrap namespace --- */
	kr = task_get_bootstrap_port(mach_task_self(), &bootstrap);
	printf("XPCUNCONN bootstrap_port kr=%d port=0x%x\n", (int)kr, (unsigned)bootstrap);
	if (kr == KERN_SUCCESS) {
		kr = bootstrap_look_up(bootstrap, WORKLOAD_SERVICE, &service_port);
		printf("XPCUNCONN lookup name=%s kr=%d port=0x%x\n", WORKLOAD_SERVICE, (int)kr,
				(unsigned)service_port);
	}

	/* --- control 2: is the bootstrap path itself alive for this process? A name that is unique to
	 *     this run is looked up (must fail), then checked in (registers a receive right with launchd)
	 *     and looked up again. If the check-in works, the second lookup MUST find it: that separates
	 *     "no receiver is registered for the workload's service" from "this process cannot use the
	 *     bootstrap namespace at all". A look-up of a name with no receiver is expected to fail, so
	 *     that arm is printed and NOT asserted. The result of control 1 must not gate this: its whole
	 *     point is to be an independent reading. --- */
	if (kr == KERN_SUCCESS || bootstrap != MACH_PORT_NULL) {
		mach_port_t found = MACH_PORT_NULL;
		kern_return_t probe_kr;

		snprintf(unique_service, sizeof(unique_service), "com.apple.darling.xpcunconn.missing.%d", (int)getpid());
		snprintf(checked_in_service, sizeof(checked_in_service), "com.apple.darling.xpcunconn.registered.%d", (int)getpid());
		service_port = MACH_PORT_NULL;
		probe_kr = bootstrap_look_up(bootstrap, unique_service, &service_port);
		printf("XPCUNCONN lookup name=%s kr=%d port=0x%x\n", unique_service, (int)probe_kr,
				(unsigned)service_port);

		service_port = MACH_PORT_NULL;
		checkin_kr = bootstrap_check_in(bootstrap, checked_in_service, &service_port);
		printf("XPCUNCONN checkin name=%s kr=%d port=0x%x\n", checked_in_service, (int)checkin_kr,
				(unsigned)service_port);

		if (checkin_kr == KERN_SUCCESS) {
			kr = bootstrap_look_up(bootstrap, checked_in_service, &found);
			printf("XPCUNCONN lookup_after_checkin name=%s kr=%d port=0x%x\n", checked_in_service,
					(int)kr, (unsigned)found);
			if (kr != KERN_SUCCESS) {
				fail("a service this process just checked in could not be looked up again");
			}
		}
	}

	/* --- subject A: a synchronous send on a connection that is NEVER RESUMED --- */
	snprintf(unique_service, sizeof(unique_service), "com.apple.darling.xpcunconn.%d", (int)getpid());
	printf("XPCUNCONN step=create_never_resumed name=%s\n", unique_service);
	connection = xpc_connection_create_mach_service(unique_service, NULL, 0);
	if (connection == NULL) {
		fail("xpc_connection_create_mach_service returned NULL");
	} else {
		message = make_message();
		printf("XPCUNCONN step=send_sync_never_resumed_begin\n");
		reply = xpc_connection_send_message_with_reply_sync(connection, message);
		printf("XPCUNCONN step=send_sync_never_resumed_returned\n");
		if (reply != NULL && xpc_get_type(reply) != XPC_TYPE_ERROR) {
			/* A receiver for a name no launchd job can declare is not a pass, it is a lie. */
			fail("the never-resumed sync send did not report an error");
		}
		check_returned("never_resumed", reply);
	}

	/* --- subject B: the workload's own case -- resumed, and activation fails (no receiver) --- */
	printf("XPCUNCONN step=create_workload_service name=%s\n", WORKLOAD_SERVICE);
	connection = xpc_connection_create_mach_service(WORKLOAD_SERVICE, NULL, 0);
	if (connection == NULL) {
		fail("xpc_connection_create_mach_service returned NULL for the workload's service");
	} else {
		/* No event handler is installed and no block is used on purpose: nothing in this oracle may
		 * depend on a delivery that cannot happen for a service with no receiver. */
		printf("XPCUNCONN step=resume_workload_service\n");
		xpc_connection_resume(connection);
		message = make_message();
		printf("XPCUNCONN step=send_sync_resumed_begin\n");
		reply = xpc_connection_send_message_with_reply_sync(connection, message);
		printf("XPCUNCONN step=send_sync_resumed_returned\n");
		check_returned("resumed_missing_service", reply);
	}

	/* --- control 2: the runtime's own mach message path still works --- */
	printf("XPCUNCONN step=mach_round_trip\n");
	control_mach_round_trip();

	/* Reaching this line is the "none of the above killed the process" evidence. */
	printf("XPCUNCONN survived=1 failures=%d\n", failures);
	if (failures == 0) {
		printf("XPC_SYNC_SEND_UNCONNECTED_OK\n");
		return 0;
	}
	printf("XPC_SYNC_SEND_UNCONNECTED_FAIL\n");
	return 1;
}
