/*
 * fcd_sync_send_park_guest.c -- follow-up 8's peer test: does a SYNCHRONOUS XPC send to a Mach service
 * whose daemon CANNOT START park the caller, in this prefix?
 *
 * WHY THIS EXISTS. The stall was re-measured from both sides: xcodebuild parks in IDEInitialize in an
 * ordinary libdispatch channel receive, its own words stop at the IDELogStore log-store failures, and
 * SWBBuildService is registered and idle (never asked). The libxpc sync-send candidate is fixed and
 * refuted (tests/xpc_sync_send_unconnected_guest.c is GREEN). What is left is WHICH PEER the parked
 * channel belongs to. MEASURED, from the daemon's own log at the arm's stall second (14:47:24), the peer
 * is `com.apple.FileCoordination`: daemon.m logs `new client` and `received message from client`, and
 * FileAccessRequest.m walks the request through its queue to `did finish waiting for presenters`. The
 * daemon IS deployed (libexec/darling/usr/sbin/filecoordinationd) -- this test exists because the
 * CLIENT side is what parks: NSFileCoordinator.m sends the intent asynchronously and then blocks its
 * calling thread on `dispatch_semaphore_wait(waiter, DISPATCH_TIME_FOREVER)` twice, once for the
 * daemon's intent reply and once for the completion acknowledgement.
 *
 * THE CONTRACT. Every message a client sends must be ANSWERED (an error object is a fine answer; silence
 * is not), and the two-phase intent protocol must complete. So this prints `FCD_NO_PARK_OK` when all
 * three phases return, and on a park the SIGALRM handler prints `FCD-VERDICT ... parked=1` and exits 2,
 * which is the RED arm.
 */
#include <mach/mach.h>
#include <servers/bootstrap.h>
#include <dispatch/dispatch.h>
#include <xpc/xpc.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#define FCD_SERVICE "com.apple.FileCoordination"

static volatile int in_send;

static void on_alarm(int sig)
{
	(void) sig;
	printf("FCD-VERDICT service=%s parked=%d (the synchronous send never returned)\n",
		FCD_SERVICE, in_send ? 1 : 0);
	fflush(stdout);
	_exit(2);
}

int main(void)
{
	mach_port_t bootstrap_port = MACH_PORT_NULL;
	mach_port_t service_port = MACH_PORT_NULL;
	kern_return_t kr;

	signal(SIGALRM, on_alarm);
	alarm(20);

	kr = task_get_bootstrap_port(mach_task_self(), &bootstrap_port);
	printf("FCD bootstrap_port kr=%d port=0x%x\n", kr, bootstrap_port);

	kr = bootstrap_look_up(bootstrap_port, FCD_SERVICE, &service_port);
	printf("FCD lookup name=%s kr=%d port=0x%x\n", FCD_SERVICE, kr, service_port);

	xpc_connection_t conn = xpc_connection_create_mach_service(FCD_SERVICE, NULL, 0);
	printf("FCD create service=%s conn=%p\n", FCD_SERVICE, (void*) conn);
	if (conn == NULL) {
		printf("FCD-VERDICT parked=0 create-failed\n");
		printf("FCD_NO_PARK_OK\n");
		return 0;
	}
	xpc_connection_resume(conn);

	xpc_object_t message = xpc_dictionary_create(NULL, NULL, 0);
	printf("FCD send-begin (empty dictionary: the daemon must still answer)\n");
	fflush(stdout);
	in_send = 1;
	xpc_object_t reply = xpc_connection_send_message_with_reply_sync(conn, message);
	in_send = 0;
	alarm(0);

	printf("FCD send-returned reply=%p type=%d\n", (void*) reply,
		reply ? (int) xpc_get_type(reply) : -1);
	printf("FCD-VERDICT service=%s lookup_kr=%d parked=0\n", FCD_SERVICE, kr);

	// THE CLIENT SIDE OF THE PRESENTER HANDSHAKE (follow-up 11). The daemon does not answer an intent
	// until the client has answered the presenter notification it sends for that path
	// (NSFileCoordinator+Internal.h: DaemonMessageTypePresenterNotification must come back as
	// DaemonMessageTypePresenterReply with a "responses" array whose items carry the notification's own
	// type and DaemonPresenterReplyItemResultOk -- exactly what NSFileCoordinator.m's
	// handle_xpc_notification does). Without a handler the daemon correctly waits forever, so the phases
	// below could only ever park. Installing it AFTER phase 1 keeps phase 1's no-handler case in the test:
	// phase 1's answer arrives as the reply to a synchronous send and never needs a handler, while an
	// UNSOLICITED message on a handler-less connection was a real libxpc defect (it invoked a NULL
	// handler, measured: rdi=0 inside dispatch_mach_handler) which this rung fixed.
	xpc_connection_set_event_handler(conn, ^(xpc_object_t object) {
		xpc_type_t type = xpc_get_type(object);
		if (type != XPC_TYPE_DICTIONARY) {
			printf("FCD-HANDLER non-dictionary type=%ld\n", (long) type);
			fflush(stdout);
			return;
		}
		uint64_t mtype = xpc_dictionary_get_uint64(object, "type");
		printf("FCD-HANDLER message type=%llu\n", (unsigned long long) mtype);
		fflush(stdout);
		if (mtype == 8 /* DaemonMessageTypePresenterNotification */) {
			xpc_object_t notifications = xpc_dictionary_get_value(object, "notifications");
			xpc_object_t presenterReply = xpc_dictionary_create_reply(object);
			if (presenterReply == NULL) {
				printf("FCD-HANDLER presenter notification carries no reply port\n");
				fflush(stdout);
				return;
			}
			xpc_object_t responses = xpc_array_create(NULL, 0);
			xpc_array_apply(notifications, ^bool(size_t index, xpc_object_t notification) {
				(void) index;
				xpc_object_t item = xpc_dictionary_create(NULL, NULL, 0);
				xpc_dictionary_set_uint64(item, "notification-type",
					xpc_dictionary_get_uint64(notification, "notification-type"));
				xpc_dictionary_set_uint64(item, "result", 1 /* DaemonPresenterReplyItemResultOk */);
				xpc_array_append_value(responses, item);
				xpc_release(item);
				return true;
			});
			xpc_dictionary_set_uint64(presenterReply, "type", 9 /* PresenterReply */);
			xpc_dictionary_set_value(presenterReply, "responses", responses);
			xpc_release(responses);
			xpc_connection_send_message(conn, presenterReply);
			xpc_release(presenterReply);
			printf("FCD-HANDLER presenter-notification answered\n");
			fflush(stdout);
		}
	});

	// Phase 2: a WELL-FORMED intent, the shape NSFileCoordinator sends (NSFileCoordinator+Internal.h:
	// DaemonMessageTypeIntent = 1, keys "path"/"options"/"cancellation-token"/"purpose-identifier").
	// The client then waits on a semaphore for the daemon's intent reply, so if this parks, the peer's
	// grant path is the wall; if it returns, the client's second wait (the completion ack) is.
	alarm(25);
	xpc_object_t intent = xpc_dictionary_create(NULL, NULL, 0);
	xpc_dictionary_set_uint64(intent, "type", 1);
	xpc_dictionary_set_string(intent, "path", "/tmp/fcd-peer-test");
	xpc_dictionary_set_uint64(intent, "options", 0);
	xpc_dictionary_set_string(intent, "cancellation-token", "fcd-peer-test-token");
	xpc_dictionary_set_string(intent, "purpose-identifier", "com.apple.darling.fcdpeer");
	printf("FCD2 intent-send-begin\n");
	fflush(stdout);
	in_send = 1;
	xpc_object_t intentReply = xpc_connection_send_message_with_reply_sync(conn, intent);
	in_send = 0;
	alarm(0);
	printf("FCD2 intent-reply=%p type=%d result=%llu\n", (void*) intentReply,
		intentReply ? (int) xpc_get_type(intentReply) : -1,
		intentReply ? (unsigned long long) xpc_dictionary_get_uint64(intentReply, "result") : 0ULL);

	// Phase 3: the EXACT shape NSFileCoordinator uses (async send with a reply handler + a semaphore the
	// calling thread waits on forever). If phase 2 returns but this parks, the wall is the DELIVERY of
	// the reply to the client's connection/queue, not the daemon's grant.
	alarm(25);
	dispatch_semaphore_t waiter = dispatch_semaphore_create(0);
	__block int got = 0;
	__block uint64_t gotResult = 0;
	xpc_object_t intent2 = xpc_dictionary_create(NULL, NULL, 0);
	xpc_dictionary_set_uint64(intent2, "type", 1);
	xpc_dictionary_set_string(intent2, "path", "/tmp/fcd-peer-test-async");
	xpc_dictionary_set_uint64(intent2, "options", 0);
	xpc_dictionary_set_string(intent2, "cancellation-token", "fcd-peer-test-token-async");
	xpc_dictionary_set_string(intent2, "purpose-identifier", "com.apple.darling.fcdpeer");
	printf("FCD3 async-intent-send-begin\n");
	fflush(stdout);
	in_send = 1;
	// The reply queue must be one this single-threaded test can actually run. The main queue is serviced
	// by the main thread, and the main thread is parked in the semaphore wait below, so a reply delivered
	// to the main queue could never be handled -- a test artifact, not a libxpc defect (NSFileCoordinator
	// does this from inside a running main queue/run loop). A global queue keeps the shape the phase is
	// about (async send + reply handler + a semaphore the calling thread waits on forever) and lets the
	// DELIVERY be measured rather than pre-determined by the test's own structure.
	dispatch_queue_t replyQueue = dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0);
	xpc_connection_send_message_with_reply(conn, intent2, replyQueue, ^(xpc_object_t reply3) {
		got = 1;
		gotResult = reply3 ? xpc_dictionary_get_uint64(reply3, "result") : 0;
		printf("FCD3 async-reply type=%d result=%llu\n", reply3 ? (int) xpc_get_type(reply3) : -1,
			(unsigned long long) gotResult);
		fflush(stdout);
		dispatch_semaphore_signal(waiter);
	});
	dispatch_semaphore_wait(waiter, DISPATCH_TIME_FOREVER);
	in_send = 0;
	alarm(0);
	printf("FCD3 async-reply-received got=%d\n", got);

	printf("FCD_NO_PARK_OK\n");
	fflush(stdout);
	return 0;
}
