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
 * The first case carries a plain message (no MACH_MSGH_BITS_COMPLEX), so it never reaches
 * ipc_kmsg_copyout_body()/ipc_kmsg_copyout_port_descriptor() -- the helpers .4b had to thread the
 * requester through. The second case covers that gap: one MACH_MSG_PORT_DESCRIPTOR is sent to the
 * EVFILT_MACHPORT receive port and received through the same kqchan read/fill path.
 *
 * Host-built as a guest Mach-O and executed by the prefix; nothing is compiled in the guest.
 */
#include <sys/event.h>
#include <sys/time.h>
#include <unistd.h>
#include <mach/mach.h>
#include <mach/mach_port.h>
#include <mach/message.h>
#include <stdio.h>
#include <string.h>
#include <stdint.h>

#define FILL_MSG_ID 0x4b46494c /* 'KFL' */
#define DESC_MSG_ID 0x4b464944 /* 'KFD' */

struct fill_message {
	mach_msg_header_t header;
	char payload[16];
};

/* A port descriptor: header + body + one port descriptor, no trailer. */
struct desc_message {
	mach_msg_header_t header;
	mach_msg_body_t body;
	mach_msg_port_descriptor_t desc;
};

/*
 * Descriptor-bearing case (.4b regression seam for the descriptor copyout helpers).
 *
 * The message carries one MACH_MSG_PORT_DESCRIPTOR whose disposition is COPY_SEND. Receiving it must
 * (a) succeed through the kqchan read/fill path, (b) lay the descriptor out for the requester (a guest
 * task, not kernel_task) with the port's translated name, and (c) materialize that right in the
 * requester's own IPC space.
 *
 * The kqueue read reply names the buffer the server copied the message into (kev->ext[0], the knote's
 * default buffer) and the received size (kev->ext[1]); the guest reads it to inspect exactly what the
 * server laid out. That buffer is guest memory (libkqueue's knote), so a mislaid descriptor is visible
 * here: a zeroed/garbage name fails mach_port_type(), and a copyout that did not reach the requester's
 * space leaves the payload's send-right reference count unchanged.
 */
static int descriptor_case(void) {
	int kq = kqueue();
	if (kq < 0) {
		perror("kqueue(desc)");
		return 1;
	}

	mach_port_t rport = MACH_PORT_NULL;
	kern_return_t kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &rport);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: mach_port_allocate(receive): %d\n", (int)kr);
		return 1;
	}
	kr = mach_port_insert_right(mach_task_self(), rport, rport, MACH_MSG_TYPE_MAKE_SEND);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: insert_right(rport): %d\n", (int)kr);
		return 1;
	}

	/* The port the descriptor carries. It stays owned here so the received right's effect on the
	 * requester's space is directly observable through its reference count. */
	mach_port_t payload = MACH_PORT_NULL;
	kr = mach_port_allocate(mach_task_self(), MACH_PORT_RIGHT_RECEIVE, &payload);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: mach_port_allocate(payload): %d\n", (int)kr);
		return 1;
	}
	kr = mach_port_insert_right(mach_task_self(), payload, payload, MACH_MSG_TYPE_MAKE_SEND);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: insert_right(payload): %d\n", (int)kr);
		return 1;
	}

	mach_port_urefs_t refs_before = 0;
	kr = mach_port_get_refs(mach_task_self(), payload, MACH_PORT_RIGHT_SEND, &refs_before);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: get_refs before: %d\n", (int)kr);
		return 1;
	}

	struct kevent ev;
	EV_SET(&ev, rport, EVFILT_MACHPORT, EV_ADD, MACH_RCV_MSG, 0, NULL);
	if (kevent(kq, &ev, 1, NULL, 0, NULL) < 0) {
		perror("desc: kevent add");
		return 1;
	}

	struct desc_message msg;
	memset(&msg, 0, sizeof(msg));
	msg.header.msgh_bits = MACH_MSGH_BITS(MACH_MSG_TYPE_COPY_SEND, 0) | MACH_MSGH_BITS_COMPLEX;
	msg.header.msgh_size = sizeof(msg);
	msg.header.msgh_remote_port = rport;
	msg.header.msgh_local_port = MACH_PORT_NULL;
	msg.header.msgh_id = DESC_MSG_ID;
	msg.body.msgh_descriptor_count = 1;
	msg.desc.name = payload;
	msg.desc.disposition = MACH_MSG_TYPE_COPY_SEND;
	msg.desc.type = MACH_MSG_PORT_DESCRIPTOR;
	kr = mach_msg(&msg.header, MACH_SEND_MSG, sizeof(msg), 0, MACH_PORT_NULL, MACH_MSG_TIMEOUT_NONE, MACH_PORT_NULL);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: mach_msg send: %d\n", (int)kr);
		return 1;
	}

	/* kevent64 (rather than kevent) is used so the reply's ext[] -- which names the receive
	 * buffer the server copied into -- is available to the guest. */
	struct kevent64_s out;
	memset(&out, 0, sizeof(out));
	struct timespec timeout = { .tv_sec = 10, .tv_nsec = 0 };
	int n = kevent64(kq, NULL, 0, &out, 1, 0, &timeout);
	if (n < 0) {
		perror("desc: kevent64 wait");
		return 1;
	}
	if (n == 0) {
		fprintf(stderr, "desc: no event for the posted descriptor message\n");
		return 1;
	}
	if (out.filter != EVFILT_MACHPORT) {
		fprintf(stderr, "desc: unexpected filter: %d\n", out.filter);
		return 1;
	}
	if (out.fflags != 0) {
		fprintf(stderr, "desc: read/fill did not receive the complex message: fflags=0x%x\n", out.fflags);
		return 1;
	}

	mach_msg_header_t *rcv = (mach_msg_header_t *)(uintptr_t)out.ext[0];
	if (rcv == NULL) {
		fprintf(stderr, "desc: server did not report a receive buffer\n");
		return 1;
	}
	if ((rcv->msgh_bits & MACH_MSGH_BITS_COMPLEX) == 0) {
		fprintf(stderr, "desc: received message lost MACH_MSGH_BITS_COMPLEX: bits=0x%x\n", rcv->msgh_bits);
		return 1;
	}
	if (rcv->msgh_id != DESC_MSG_ID) {
		fprintf(stderr, "desc: wrong message id: 0x%x\n", rcv->msgh_id);
		return 1;
	}

	mach_msg_body_t *dbody = (mach_msg_body_t *)(rcv + 1);
	if (dbody->msgh_descriptor_count != 1) {
		fprintf(stderr, "desc: descriptor count: %u\n", dbody->msgh_descriptor_count);
		return 1;
	}
	mach_msg_descriptor_t *dsc = (mach_msg_descriptor_t *)(dbody + 1);
	if (dsc->type.type != MACH_MSG_PORT_DESCRIPTOR) {
		fprintf(stderr, "desc: descriptor type not port: %u\n", dsc->type.type);
		return 1;
	}

	mach_port_name_t dname = dsc->port.name;
	if (dname == MACH_PORT_NULL || dname == MACH_PORT_DEAD) {
		fprintf(stderr, "desc: descriptor carries no valid port name: %u\n", dname);
		return 1;
	}

	/* The received right must be usable in the requester's own namespace. */
	mach_port_type_t dtype = 0;
	kr = mach_port_type(mach_task_self(), dname, &dtype);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: received port name %u is not a right of this task: %d\n", dname, (int)kr);
		return 1;
	}
	if ((dtype & MACH_PORT_TYPE_SEND) == 0) {
		fprintf(stderr, "desc: received right is not a send right: type=0x%x\n", dtype);
		return 1;
	}

	/* COPY_SEND must have added exactly one send reference in the requester's space. */
	mach_port_urefs_t refs_after = 0;
	kr = mach_port_get_refs(mach_task_self(), payload, MACH_PORT_RIGHT_SEND, &refs_after);
	if (kr != KERN_SUCCESS) {
		fprintf(stderr, "desc: get_refs after: %d\n", (int)kr);
		return 1;
	}
	if (refs_after != refs_before + 1) {
		fprintf(stderr, "desc: send right not materialized in requester space (before=%u after=%u)\n",
			(unsigned)refs_before, (unsigned)refs_after);
		return 1;
	}

	/* The read path consumed the message. */
	struct desc_message drained;
	kr = mach_msg(&drained.header, MACH_RCV_MSG | MACH_RCV_TIMEOUT, 0, sizeof(drained), rport, 100, MACH_PORT_NULL);
	if (kr != MACH_RCV_TIMED_OUT) {
		fprintf(stderr, "desc: read/fill did not consume the message: mach_msg rc=%d\n", (int)kr);
		return 1;
	}

	/* Deterministic teardown of the rights this case created. */
	if (dname != payload && dname != rport) {
		mach_port_deallocate(mach_task_self(), dname);
	}
	mach_port_deallocate(mach_task_self(), payload);
	mach_port_deallocate(mach_task_self(), rport);
	close(kq);

	printf("KQCHAN_FILL_DESC_OK=1 fflags=0 complex=1 dsc_count=1 dsc_type=port send_refs_delta=1\n");
	fflush(stdout);
	return 0;
}

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

	return descriptor_case();
}
