/*
 * dar-dtape-explicit-context-6to3.5 focused behavior: exercise the kqchan Mach-port READ/FILL path.
 *
 * Registers EVFILT_MACHPORT on a receive right with MACH_RCV_MSG, posts one known message, then
 * retrieves the event. libkqueue's evfilt_machport_copyout (src/external/libkqueue/src/linux/
 * machport.c) answers a ready machport event by sending dserver_kqchan_msgnum_mach_port_read to the
 * server, which runs Kqchan::MachPort::_read -> dtape_kqchan_mach_port_fill -> filt_machportprocess
 * -> ipc_mqueue_receive_on_thread -> mach_msg_receive_results and copies the message into the
 * default buffer libkqueue handed over.
 *
 * The .4b slice removed the impersonate()/impersonate(nullptr) pair around that server call and now
 * carries the requester thread explicitly. This fixture fails if the read path no longer receives the
 * message: the event carries MACH_MSG_SUCCESS (fflags 0) only after a real copyout, and the message
 * must then be gone from the queue (a direct receive observes MACH_RCV_TIMED_OUT). If the requester's
 * task/space/map/kevent_ctx were not used, the receive would not consume the message.
 *
 * Host-built as a guest Mach-O and executed by the prefix; nothing is compiled in the guest.
 */
#include <sys/event.h>
#include <sys/time.h>
#include <mach/mach.h>
#include <mach/mach_port.h>
#include <mach/message.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>

#define FILL_MSG_ID 0x4b46494c /* 'KFL' */

struct fill_message {
	mach_msg_header_t header;
	char payload[16];
};

int main(void) {
	int kq = kqueue();
	if (kq < 0) {
		perror("kqueue");
		return 1;
	}

	mach_port_t port = MACH_PORT_NULL;
	kern_return_t kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &port);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "mach_port_allocate: %d\n", (int)kr);
		return 1;
	}

	/* We need a send right to post ourselves a message. */
	kr = mach_port_insert_right(mach_task_self(), port, port, MACH_MSG_TYPE_MAKE_SEND);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "mach_port_insert_right: %d\n", (int)kr);
		return 1;
	}

	/* A fresh ident with EV_ADD takes the kn_create path; MACH_RCV_MSG makes the read
	 * path perform a real receive (rather than only detect the port/size). */
	struct kevent ev;
	EV_SET(&ev, port, EVFILT_MACHPORT, EV_ADD, MACH_RCV_MSG, 0, NULL);
	if (kevent(kq, &ev, 1, NULL, 0, NULL) < 0) {
		perror("kevent add");
		return 1;
	}

	/* Post one known message. */
	struct fill_message msg;
	memset(&msg, 0, sizeof(msg));
	msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0);
	msg.header.msgh_size = sizeof(msg);
	msg.header.msgh_remote_port = port;
	msg.header.msgh_local_port = MACH_PORT_NULL;
	msg.header.msgh_id = FILL_MSG_ID;
	memcpy(msg.payload, "KQCHAN_FILL", 11);
	kr = mach_msg(&msg.header, MACH_SEND_MSG, sizeof(msg), 0, MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "mach_msg send: %d\n", (int)kr);
		return 1;
	}

	/* Retrieve the ready event: this drives the server kqchan read/fill. */
	struct kevent out;
	memset(&out, 0, sizeof(out));
	struct timespec timeout = { .tv_sec = 10, .tv_nsec = 0 };
	int n = kevent(kq, NULL, 0, &out, 1, &timeout);
	if (n < 0) {
		perror("kevent wait");
		return 1;
	}
	if (n == 0) {
		fprintf(stderr, "kevent wait: no event for the posted message\n");
		return 1;
	}

	if (out.filter != EVFILT_MACHPORT) {
		fprintf(stderr, "unexpected filter: %d\n", out.filter);
		return 1;
	}
	/* A successful receive reports MACH_MSG_SUCCESS (0) in fflags; a drop reports
	 * EVFILT_DROP (with code 0xdead), and a "too large" result reports MACH_RCV_TOO_LARGE. */
	if (out.fflags != 0) {
		fprintf(stderr, "read/fill did not receive the message: fflags=0x%x\n", out.fflags);
		return 1;
	}

	/* The read path performed a real receive, so the message must be gone from the queue.
	 * If it were still there, the receive was not on the requester's mqueue/space. */
	struct fill_message drained;
	kr = mach_msg(&drained.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof(drained), port, 100, MACH_PORT_NULL);
	if (kr != MACH_RCV_TIMED_OUT) {
		fprintf(stderr, "read/fill did not consume the message: mach_msg rc=%d\n", (int)kr);
		return 1;
	}

	printf("KQCHAN_FILL_OK=1 fflags=0 consumed=1\n");
	fflush(stdout);
	return 0;
}
